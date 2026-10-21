# Setup: Shopify, monday.com, Slack and n8n

This takes about 30 minutes. You need admin access to the Shopify store, a monday.com account that can create boards, and a self-hosted n8n that Shopify can reach over HTTPS.

## 1. Build the board in monday.com

1. Create a board, for example "Fulfillment".
2. Add the columns below. Titles are up to you; the column type matters.

   | Key | Suggested title | Column type |
   |---|---|---|
   | `order_number` | Order | Text |
   | `customer` | Customer | Text |
   | `email` | Email | Email |
   | `total` | Total | Numbers (set the unit to your shop currency) |
   | `payment_status` | Payment | Status |
   | `fulfillment_status` | Fulfillment | Status |
   | `shipping_method` | Shipping | Text |
   | `order_date` | Order date | Date (turn on "Include time" in the column settings) |
   | `flags` | Flags | Dropdown |
   | `shopify_updated_at` | Shopify updated | Text |

   Only `order_number` is required. Keep `shopify_updated_at`, because it is how late updates are recognised, and `flags`, because it is how the workflow remembers which alerts it already sent. You can hide both columns from the board view.

3. Add a subitem to any item, then add two subitem columns: **SKU** (Text) and **Qty** (Numbers). Delete the test subitem afterwards.
4. Add a separate status column for your team's own work, for example "Stage" with Picking, Packed and Shipped. The workflow never touches it, while it overwrites the columns above on every update.

**Labels.** The workflow creates missing status and dropdown labels by itself (`create_labels_if_missing`). That needs a monday user who can change the board's structure, usually the board owner. If your token's user cannot, add these labels by hand:

- Payment: Pending, Authorized, Partially paid, Paid, Partially refunded, Refunded, Voided, Expired
- Fulfillment: Unfulfilled, Partially fulfilled, Fulfilled, Restocked, Not eligible, Cancelled
- Flags: VIP, Rush

## 2. Collect the monday.com values

- **API token** (`MONDAY_API_TOKEN`): click your profile picture, choose **Developers**, then **API token → Show**. Admins can also use **Administration → Connections → Personal API token**. The token acts as you, so it can only reach boards you can reach.
- **Board ID** (`MONDAY_BOARD_ID`): open the board in a web browser. The number in the URL is the ID: `https://yourcompany.monday.com/boards/1234567890`.
- **Column IDs** (`MONDAY_COLUMNS`): click your profile picture, open **monday.labs** and turn on **Developer mode**. Now the menu next to each column title shows its ID, and clicking the ID copies it. IDs look like `text_mkx1ordr` or `color_mkx1paym`.
- **Subitem column IDs** (`MONDAY_SUBITEM_COLUMNS`): the same, on the SKU and Qty columns of a subitem. Subitems live on their own board, so these IDs differ from the main board's.
- **Group** (`MONDAY_GROUP_ID`, optional): new orders go to the top group unless you set this. To find a group ID, run this in the API playground of the monday Developer Center:

  ```graphql
  query { boards(ids: 1234567890) { groups { id title } } }
  ```

Put the column IDs together as one line of JSON, for example:

```
MONDAY_COLUMNS={"order_number":"text_mkx1ordr","customer":"text_mkx1cust","email":"email_mkx1mail","total":"numeric_mkx1totl","payment_status":"color_mkx1paym","fulfillment_status":"color_mkx1fulf","shipping_method":"text_mkx1ship","order_date":"date_mkx1ordd","flags":"dropdown_mkx1flag","shopify_updated_at":"text_mkx1upd8"}
MONDAY_SUBITEM_COLUMNS={"sku":"text_mkx2sku0","quantity":"numeric_mkx2qty0"}
```

## 3. Optional: a Slack incoming webhook

1. Go to [api.slack.com/apps](https://api.slack.com/apps), create an app from scratch and pick your workspace.
2. Open **Incoming Webhooks**, switch it on and click **Add New Webhook**. Choose the channel for order alerts.
3. Copy the URL into `SLACK_WEBHOOK_URL`. The error alerts use the same URL.

## 4. Prepare n8n

1. Add the variables from the README (or `.env.example`) to your n8n environment and restart n8n. Self-hosted n8n needs `N8N_BLOCK_ENV_ACCESS_IN_NODE=false`.
2. Import `workflow/shopify-orders-to-monday.json` and `workflow/error-alert-slack.json`.
3. **Activate the error alert workflow.** n8n 2.x will not run an error workflow that is not active; it only writes "is not active and cannot be executed" to its log.
4. In the order workflow's settings, choose the error alert workflow as its **Error Workflow**.
5. Activate the order workflow. Its production URL is `https://YOUR-N8N-DOMAIN/webhook/shopify-orders-to-monday`.

Shopify only delivers to public HTTPS addresses. A hosted n8n works as is. A local n8n needs a tunnel such as Cloudflare Tunnel or ngrok.

## 5. Connect Shopify

There are two ways to get order webhooks. Pick one; the workflow works the same with both.

### Option A: webhooks in the Shopify admin (simplest)

1. In the Shopify admin, go to **Settings → Notifications → Webhooks** and click **Create webhook**.
2. **Event:** Order creation. **Format:** JSON. **URL:** your production URL from step 4. **Webhook API version:** the latest stable version.
3. Save, then create a second webhook with the event **Order update** and the same URL.
4. At the bottom of the Webhooks page Shopify shows the key it signs with ("Your webhooks will be signed with ..."). Copy it into `SHOPIFY_WEBHOOK_SECRET` and restart n8n.

### Option B: an app made in the Shopify Dev Dashboard

Use this if your store already runs a custom app, or if you manage webhooks in code.

1. Give the app the `read_orders` scope and subscribe it to the `orders/create` and `orders/updated` topics with your production URL (in `shopify.app.toml` or in the Dev Dashboard).
2. Use the app's **client secret** as `SHOPIFY_WEBHOOK_SECRET`. App webhooks are signed with it.
3. Request access to **protected customer data** for the name and email fields. Without it, Shopify leaves those fields empty in the webhook, and the items show "Guest" with no email.

## 6. Try it

1. Place a test order in the store (for example with Shopify's test payment gateway).
2. Within a few seconds, the order appears on the board with its line items as subitems.
3. Mark the order as fulfilled in Shopify. The same item's Fulfillment column changes to Fulfilled, and no new item appears.
4. To test the alerts, place an order above `VIP_ORDER_TOTAL`, or choose a shipping method with one of your rush words. Slack gets one alert with a link to the item.

Shopify's **Send test** button on the Webhooks page sends a sample order. It shows up on the board like a real order, so delete it afterwards.

## Troubleshooting

Open the workflow's **Executions** list in n8n and look at where the run stopped.

| You see | Likely cause |
|---|---|
| No execution at all | The workflow is not active, the webhook URL is wrong, or Shopify cannot reach n8n over HTTPS |
| Run stopped at "Stop: Bad Signature" and the alert says "SHOPIFY_WEBHOOK_SECRET is not set in n8n" | The variable is missing or empty in n8n's environment. Add it and restart n8n; until then every webhook gets a 401 |
| Run stopped at "Stop: Bad Signature" and the alert says the header "is missing or wrong" | `SHOPIFY_WEBHOOK_SECRET` is wrong, or the request did not come from Shopify. Admin webhooks use the key on the Webhooks page, app webhooks use the app's client secret |
| "Prepare Order" failed with "Missing config" or "Unknown key" | A variable is missing, or a key in `MONDAY_COLUMNS` is misspelled. The message lists the allowed keys |
| "Monday: Find Order" failed with Unauthorized (HTTP 401) | `MONDAY_API_TOKEN` is wrong, or its user has no access to the board |
| A "Monday:" node failed with InvalidColumnIdException | A column ID does not exist on the board. Subitem IDs belong in `MONDAY_SUBITEM_COLUMNS`, not `MONDAY_COLUMNS` |
| A "Monday:" node failed with ColumnValueException | A key points at a column of the wrong type (for example `order_date` at a text column), or the token's user cannot create the missing labels |
| A "Monday:" node failed after waiting | monday's rate limit lasted longer than `MONDAY_MAX_ATTEMPTS` and `MONDAY_MAX_WAIT_SECONDS` allow. The next update of the order fills in the item |
| A "Monday:" node failed with HTTP 423 | The board stayed locked through every retry, usually because another automation writes to it heavily at the same moment. Spread that automation out or raise `MONDAY_MAX_ATTEMPTS` |
| "Monday: Add Line Items" stopped "to stay inside the n8n Code node time limit" | A very large order, or a slow monday. The lines added so far stay, and the next update of the order adds the rest |
| Items say "Guest" with no email | Your app has no protected customer data access (option B) |
| No Slack alert for a VIP order | `SLACK_WEBHOOK_URL` is empty, or the flag was already on the board |
| Runs fail but no error alert arrives | The error alert workflow is not active, or it is not chosen as the Error Workflow |
