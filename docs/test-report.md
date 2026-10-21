# Test report

**Date:** 2026-10-21
**n8n:** 2.19.5, self-hosted in Docker
**Test script:** [tests/e2e_mock_test.py](../tests/e2e_mock_test.py)
**Result:** 50 of 50 checks pass across 37 scenarios

## How it was tested

No real Shopify store, monday.com account or Slack workspace was used. The test script:

1. starts a mock monday.com GraphQL API and a mock Slack incoming webhook on the test machine,
2. imports temporary copies of the workflow and of the error alert workflow, all named `TEST shopify-orders-to-monday (temporary)`, with the monday host pointed at the mock and fake keys. Three extra copies cover config problems: one where `SHOPIFY_WEBHOOK_SECRET` is not set at all (n8n sees `undefined`, exactly like a variable nobody added), one where it is only spaces, and one with a typo in `MONDAY_COLUMNS`,
3. activates them (the error alert workflow too, since n8n 2.x only runs an active error workflow) and sets the error alert workflow as the main workflow's Error Workflow,
4. sends webhooks exactly like Shopify does: the order JSON as the body, `X-Shopify-Topic`, `X-Shopify-Webhook-Id`, `X-Shopify-Shop-Domain` and the other Shopify headers, and `X-Shopify-Hmac-Sha256` as the base64 HMAC SHA256 of the raw body,
5. waits for each n8n run to finish, then checks every request the workflow made, the state of the mock board and the node each run ended on,
6. deletes all temporary copies.

### How strict the mock monday API is

The mock is a small working board: items, subitems, status and dropdown labels and groups persist between requests, so later scenarios see what earlier ones wrote. Each request goes through the same gates a real monday request would:

| Check | What the mock does when it fails |
|---|---|
| `Authorization` holds the token (no `Bearer` prefix) | 401 |
| JSON body with a `query` string | 400 |
| Query parses as GraphQL | 200 with a `Parse error` in `errors` |
| Query is valid against the schema subset (fields, argument names and types, declared and used variables) | 200 with validation errors |
| `API-Version` header in `YYYY-MM` format | 200 with `InvalidVersionException` |
| `column_values` sent as a JSON string, column IDs exist on the board | 200 with `InvalidColumnIdException` |
| Values in monday's formats: plain string for text, number as a string, `{"label"}` for status, `{"date","time"}` for dates, `{"email","text"}` for email, `{"labels"}` for dropdowns | 200 with `ColumnValueException` |
| Status and dropdown labels exist, unless `create_labels_if_missing` is true | 200 with `ColumnValueException` |
| `Idempotency-Key` on mutations | 409 `IDEMPOTENCY_CONFLICT` with `Retry-After` while the first request runs, then the saved answer with `Idempotency-Replayed: true` |

The schema subset (`create_item`, `create_subitem`, `change_multiple_column_values`, `items_page_by_column_values` and the `Item` fields read) was compared with monday's published schema for API version 2026-07, and the argument names, types and nullability match. The last check of the run confirms that none of the 96 monday requests the workflow sent tripped any of these gates by accident; every failure in the run was injected on purpose. The request count moves by one or two between runs, because the race scenario retries a 409 a different number of times.

Faults injected on purpose: two `429 COMPLEXITY_BUDGET_EXHAUSTED` answers with `retry_in_seconds: 1`, a `423 Resource is currently locked` with `Retry-After: 1`, a `504` after `create_item` already ran (a lost answer), a `ColumnValueException` inside an HTTP 200 answer, a failing second subitem, a `500` outage, a `401`, a 1.2 second delay on `create_item` to make two runs race, and a Slack outage (`503`).

## Findings while building

Three things came up during testing and shaped the first design:

| Finding | What changed |
|---|---|
| n8n rewrites a Code node error to the text after its **last colon**. "monday Create Item for #1010 failed: ColumnValueException: invalid value" reached the alert as just "invalid value", without the order number. Real monday messages often contain a URL, so they would be cut too | Every error thrown in a Code node goes through a small helper that removes colons and line breaks, so the alert keeps the order number and monday's error code |
| n8n 2.x does not run an error workflow that is not active. The log says `Workflow ... is not active and cannot be executed` and no alert goes out | The setup steps say to activate the error alert workflow, and the test activates it |
| Alerts from the error workflow arrive a moment after the failed run, so a check could pick up the alert of the run before | The test matches each alert to its run through the execution link inside the alert |

## Findings in review

A second pass compared every call with the official Shopify and monday.com docs and probed the workflow with requests the first test did not send. Five defects came out of it. Each one now has a check, and the same test run against the workflow from before the review fails exactly those checks (43 of 50 pass there, 50 of 50 now).

| Defect | Before the fix | Check that proves the fix |
|---|---|---|
| With `SHOPIFY_WEBHOOK_SECRET` not set at all, the Crypto node threw on the undefined key. n8n then answered **200 with an empty body**, so Shopify counted every order as delivered and never resent it. The first test missed it because its copy turned the missing variable into an empty string | `200` for every webhook, run failed at Sign Raw Body with a Node.js message about the key type | Webhook secret not set: `401` for all three requests, and the alert says the secret is not set. Webhook secret of only spaces: treated as not set |
| A shopper's name went into the Slack alert as is. A name like `<!channel>` pings the whole channel, and `<https://...\|Refund here>` shows up as a link | `Customer: <!channel> Pat & <https://phish.example.com\|Refund here> Lee` | Slack markup in the customer name: `&`, `<` and `>` are escaped |
| An order edit that swaps the shipping method keeps the old shipping line with `is_removed: true`. It was still read, so the order stayed Rush and the board said "Express Overnight, Standard Shipping" | flags `['Rush']`, shipping `'Express Overnight, Standard Shipping'` | Order edit: the Rush flag and Express are gone |
| Subitems used `quantity`, the quantity at checkout. After an order edit, `current_quantity` is the real one, and a removed line has `current_quantity` 0 | Tote with quantity 3 and a removed mug both added | Edited order reaches the board: one tote with quantity 1 |
| monday answers **423 Resource is currently locked** when another write holds the board. The client did not retry it, so the run failed | update statuses `[423]`, run failed | Board locked: `[423, 200]` after waiting `Retry-After` |

The fix for the missing secret trims the value in both the signing step and the check. That matters: in a test variant where only the signing step trimmed it, a secret of only spaces let a request signed with an empty key through (`200`, item created). The spaces-only check catches that variant.

Two smaller changes came with the review: the time budget in each "Monday:" step now counts request time as well as waits, so a slow monday makes the step stop with a clear message before n8n's 5 minute Code node limit (`N8N_RUNNERS_TASK_TIMEOUT`, default 300 seconds), and all temporary workflows now carry exactly the name `TEST shopify-orders-to-monday (temporary)`. If a scenario crashes because a broken workflow left it without data, the test now records a FAIL and still prints every result.

## Results

```
Forged webhook, wrong signature
  PASS  refused with 401, nothing sent to monday  (401, ended at Stop: Bad Signature)
  PASS  error workflow posts the alert to Slack  (:rotating_light: *TEST shopify-orders-to-monday (temporary)* failed at *Stop: Bad Signature*)

Forged webhook, no signature
  PASS  refused with 401, nothing sent to monday  (401, ended at Stop: Bad Signature)
  PASS  error workflow posts the alert to Slack  (:rotating_light: *TEST shopify-orders-to-monday (temporary)* failed at *Stop: Bad Signature*)

Forged webhook, body changed after signing
  PASS  refused with 401, nothing sent to monday  (401, ended at Stop: Bad Signature)
  PASS  error workflow posts the alert to Slack  (:rotating_light: *TEST shopify-orders-to-monday (temporary)* failed at *Stop: Bad Signature*)

Forged webhook with a Slack link in its topic header
  PASS  refused, and the header text never reaches Slack  (401, the alert says "topic not given")

Webhook secret not set (variable missing)
  PASS  fails closed: 401 for an empty key signature, no signature and the real signature  (empty-key 401 at Stop: Bad Signature, none 401 at Stop: Bad Signature, valid 401 at Stop: Bad Signature)
  PASS  each alert says the secret is not set  (Shopify webhook rejected: SHOPIFY_WEBHOOK_SECRET is not set in n8n, so every webhook is re)

Webhook secret of only spaces
  PASS  treated as not set: 401 for a request signed with an empty key  (401, ended at Stop: Bad Signature)

New order #1001
  PASS  Shopify gets 200 OK in under a second (its limit is 5 seconds)  (200 'OK' in 21 ms)
  PASS  looked up by order number, then one item created  (1 search, 1 create)
  PASS  item name, group and every column value exactly as mapped  (all 9 columns match)
  PASS  monday headers: token, API-Version 2026-07, JSON, Idempotency-Key on writes only  (4 requests checked)
  PASS  one subitem per line item with name, SKU and quantity  ([["Canvas Tote Bag - Natural", "NWG-TOTE-NAT", "2"], ["Stoneware Mug - Slate", "NWG-MUG-SLT", "1"]])
  PASS  not flagged, so no Slack alert  (0 alerts)

Same delivery again (Shopify retry)
  PASS  skipped: 200, no monday calls  (ended at Skip: Repeat or Other Topic)

Order #1001 fulfilled (orders/updated)
  PASS  the same item is updated, nothing new created  (1 update, 0 creates)
  PASS  fulfillment and Shopify update time changed on the board  (fulfillment Fulfilled)

Older update arrives late
  PASS  skipped, the board keeps the newer state  (ended at Skip: Older Update)

VIP order #1002 (total 622.00)
  PASS  flagged VIP on the board and in Slack with a link to the item  (*VIP order #1002*)

Rush order #1003 (Express Overnight)
  PASS  flagged Rush, Slack says why  (*Rush order #1003*)

Shipping "Expressway Freight Ground"
  PASS  keywords match whole words only, so no rush flag  (flags None)

VIP order #1002 fulfilled
  PASS  no second alert for a flag already on the board  (0 alerts)

Order #1005 upgraded to Express Overnight
  PASS  one alert: the order is now Rush  (*Order #1005 is now Rush*)

VIP limit boundary
  PASS  a total of exactly 500.00 is VIP, 499.99 is not  (#1017 ['VIP'], #1018 None)

Shipping "USPS PRIORITY MAIL"
  PASS  rush keyword "priority" matches in any case  (*Rush order #1019*)

Order edit: Express replaced by Standard, a line removed, a line added
  PASS  Rush flag and Express gone, only the new line added, no alert  (flags None, shipping 'Standard Shipping', 3 subitems)

Edited order reaches the board for the first time
  PASS  subitems use the current quantity and skip removed lines  ([["Canvas Tote Bag - Natural", "NWG-TOTE-NAT", "1"]])

Slack markup in the customer name
  PASS  escaped: no channel ping and no hidden link in the alert  (Customer: &lt;!channel&gt; Pat &amp; &lt;https://phish.example.com|Refund here&gt; Lee (pat.lee@example.com))

Update for an order not on the board
  PASS  created with all its line items  (1 item, 2 subitems)

Two items already have order #1014
  PASS  the oldest one is updated and Slack asks for a cleanup  (*Order #1014 has duplicate items on the board*)

Create and update race (#1007)
  PASS  one item and one set of subitems, never two  (1 item, 2 subitems, create attempts 3)
  PASS  the newer payload wins (payment Paid) and the VIP alert is sent once  (payment Paid, 1 alert)
  PASS  Shopify got its 200 before monday finished  (35 ms, 38 ms (create_item took 1.2 s))

monday rate limit (429 COMPLEXITY_BUDGET_EXHAUSTED twice)
  PASS  waits retry_in_seconds, retries, then syncs once  (search statuses [429, 429, 200], gaps [1.0, 1.0] s)

Board locked (HTTP 423) during an update
  PASS  waits Retry-After, retries once, then the update lands  (update statuses [423, 200], gap [1.0] s)

Answer to create_item lost (504)
  PASS  retried with the same Idempotency-Key: one item, VIP alert still sent  (create statuses [504, 200], 1 item)

GraphQL error with HTTP 200 (ColumnValueException)
  PASS  treated as a failure, not retried, nothing half written  (ended at Monday: Create Item: monday Create Item for #1010 failed with ColumnValueException (HTTP 200). invalid value, p)
  PASS  error workflow posts the alert to Slack  (:rotating_light: *TEST shopify-orders-to-monday (temporary)* failed at *Monday: Create Item*)

Line item 2 of 3 fails
  PASS  run flagged at Add Line Items, line 1 already on the board  (ended at Monday: Add Line Items, 1 subitem)

Next update of #1011
  PASS  adds the two missing lines and does not repeat the first  (["Canvas Tote Bag - Natural", "Stoneware Mug - Slate", "Soy Candle - Cedar"])

monday down (HTTP 500)
  PASS  retried 3 times, run flagged, alert sent  (3 attempts, ended at Monday: Find Order)

monday rejects the token (HTTP 401)
  PASS  not retried, run flagged  (1 attempt, ended at Monday: Find Order)

Guest checkout, no shipping, accented name, pretty printed JSON
  PASS  signature still valid, customer from billing name, "No shipping"  (#1013 José Martínez)

Order #1003 cancelled and refunded
  PASS  board shows Cancelled and Refunded, no alert  (Cancelled, Refunded, total 0.00)

Other topic (products/update)
  PASS  answered 200 and ignored  (ended at Skip: Repeat or Other Topic)

Slack down when a VIP order arrives
  PASS  the item is on the board, Slack retried 3 times, run flagged  (3 Slack attempts, ended at Slack: Order Alert)

Typo in MONDAY_COLUMNS
  PASS  clear config error, nothing sent to monday  (Unknown key ordernumber in MONDAY_COLUMNS. The allowed keys are order_)

Every monday request
  PASS  valid against the schema, column formats, labels and headers  (96 requests, 0 rejected)

50/50 checks passed across 37 scenarios (96 monday requests)
```

In the race scenario, "create attempts 3" means both runs called `create_item` with the same idempotency key: one created the item, the other got a 409 while that was running and the saved answer on its retry.

## What the workflow sent

Taken from the same run (`SHOW_REQUESTS=1`). The column IDs are the test board's.

**monday: find the order** (`items_page_by_column_values`)

```graphql
query FindOrder($board: ID!, $column: String!, $value: String!, $columnIds: [String!], $subitemColumnIds: [String!]) {
  items_page_by_column_values(board_id: $board, limit: 10, columns: [{ column_id: $column, column_values: [$value] }]) {
    items { id name url column_values(ids: $columnIds) { id text } subitems { id name column_values(ids: $subitemColumnIds) { id text } } }
  }
}
```

```json
{"board": "5000000001", "column": "text_mkx1ordr", "value": "#1001", "columnIds": ["text_mkx1ordr", "dropdown_mkx1flag", "text_mkx1upd8"], "subitemColumnIds": ["text_mkx2sku0"]}
```

**monday: create the item** (`create_item`, header `Idempotency-Key: shopify-order:5000000001:7100000001001`). `column_values` is sent as a JSON string, as monday expects:

```json
{
  "board": "5000000001",
  "name": "#1001 Jamie Rivera",
  "values": "{\"text_mkx1ordr\":\"#1001\",\"text_mkx1cust\":\"Jamie Rivera\",\"email_mkx1mail\":{\"email\":\"jamie.rivera@example.com\",\"text\":\"jamie.rivera@example.com\"},\"numeric_mkx1totl\":\"72.90\",\"color_mkx1paym\":{\"label\":\"Paid\"},\"color_mkx1fulf\":{\"label\":\"Unfulfilled\"},\"text_mkx1ship\":\"Standard Shipping\",\"date_mkx1ordd\":{\"date\":\"2026-10-20\",\"time\":\"13:15:02\"},\"text_mkx1upd8\":\"2026-10-20T13:15:02Z\"}",
  "columnIds": ["dropdown_mkx1flag", "text_mkx1upd8"],
  "group": "new_orders"
}
```

The order was placed at 09:15:02 New York time (`-04:00`), so the date column gets 13:15:02 UTC, which monday shows in each user's own time zone.

**monday: add a line item** (`create_subitem`, one per line, each with its own idempotency key)

```json
{"parent": "9100000001", "name": "Canvas Tote Bag - Natural", "values": "{\"text_mkx2sku0\":\"NWG-TOTE-NAT\",\"numeric_mkx2qty0\":\"2\"}"}
```

**monday: update after fulfillment** (`change_multiple_column_values`). The flags column is sent as `null`, which clears it, because this order has no flags:

```json
{
  "board": "5000000001",
  "item": "9100000001",
  "values": "{\"text_mkx1ordr\":\"#1001\",\"text_mkx1cust\":\"Jamie Rivera\",\"email_mkx1mail\":{\"email\":\"jamie.rivera@example.com\",\"text\":\"jamie.rivera@example.com\"},\"numeric_mkx1totl\":\"72.90\",\"color_mkx1paym\":{\"label\":\"Paid\"},\"color_mkx1fulf\":{\"label\":\"Fulfilled\"},\"text_mkx1ship\":\"Standard Shipping\",\"date_mkx1ordd\":{\"date\":\"2026-10-20\",\"time\":\"13:15:02\"},\"dropdown_mkx1flag\":null,\"text_mkx1upd8\":\"2026-10-20T19:40:11Z\"}"
}
```

**Slack: VIP alert**

```
*VIP order #1002*

Customer: Taylor Brooks (taylor.brooks@example.com)
Total: 622.00 USD
Shipping: Standard Shipping
Payment: Paid
Line items: 3 (7 units)
Why: total 622.00 is at or above the VIP limit of 500
Open in monday.com: https://northwind-goods-test.monday.com/boards/5000000001/pulses/9100000004
```

**Slack: rush alert**

```
*Rush order #1003*

Customer: Sam Cole (sam.cole@example.com)
Total: 46.50 USD
Shipping: Express Overnight
Payment: Paid
Line items: 1 (1 unit)
Why: shipping "Express Overnight" matches a rush keyword
Open in monday.com: https://northwind-goods-test.monday.com/boards/5000000001/pulses/9100000008
```

**Slack: duplicate items on the board**

```
*Order #1014 has duplicate items on the board*
Note: 2 items have order number #1014. Item 9100000029 was updated. Please merge or delete item 9100000030.
```

**Slack: error workflow alert** (the run that hit the GraphQL error)

```
:rotating_light: *TEST shopify-orders-to-monday (temporary)* failed at *Monday: Create Item*
monday Create Item for #1010 failed with ColumnValueException (HTTP 200). invalid value, please check our API documentation for the correct data structure for this column [line 60]
http://localhost:5678/workflow/dhXNUE5TyXvkbWZn/executions/6371
```

The store, customers, products and board above are made up.
