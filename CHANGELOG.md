# Changelog

## v1 (2026-10-20)

First build, reviewed and fixed on 2026-10-21 before release. Tested end to end against a mock monday.com GraphQL API and a mock Slack webhook (37 scenarios, 50 checks).

### Fixed in review (2026-10-21)

A second pass compared every call with the official Shopify and monday docs. Each fix below has a test check, and the workflow from before the fix fails those checks.

- **A missing secret made n8n accept every webhook.** With `SHOPIFY_WEBHOOK_SECRET` not set at all, the signing step crashed on the undefined key and n8n answered 200 with an empty body, so Shopify counted every order as delivered. The first test missed it because its copy turned the missing variable into an empty string. Now every webhook gets a 401 and the alert says the secret is not set. The signing step and the check both trim the value, so a secret of only spaces counts as not set too. The test now leaves the variable truly unset and also tries a spaces-only secret.
- **Shopper text reached Slack as live markup.** A customer name like `<!channel>` would ping the channel, and `<https://...|text>` would show a hidden link. `&`, `<` and `>` are now escaped in every value that comes from the order.
- **Order edits kept the old shipping method.** Shopify keeps a replaced shipping line with `is_removed: true`. It was still read, so a downgraded order stayed Rush and the board listed both methods. Removed lines are now left out.
- **Subitems used the checkout quantity.** They now use `current_quantity`, and lines removed from the order before its item is made get no subitem.
- **A locked board failed the run.** monday answers 423 "Resource is currently locked" while another write holds the board. It is now retried after `Retry-After`.
- The time budget of each "Monday:" step counts request time as well as waits, so a slow monday makes the step stop with a clear message before n8n's 5 minute Code node limit.
- Test: every temporary workflow is named `TEST shopify-orders-to-monday (temporary)`, the forged webhook checks require the error alert for all three variants, new checks cover the exact VIP limit, rush keywords in any case and the response time, and a crash in a scenario is reported as a FAIL instead of hiding the other results.

### Added

- One n8n webhook for Shopify's `orders/create` and `orders/updated` topics, answered with 200 before any monday call so Shopify never times out.
- A signature check on every webhook: `X-Shopify-Hmac-Sha256`, base64 HMAC SHA256 of the raw body. A missing or wrong signature gets a 401 and an alert, and with no secret configured every webhook is refused.
- Repeat deliveries skipped by `X-Shopify-Webhook-Id` (kept for 48 hours).
- The order looked up by order number with `items_page_by_column_values`. New orders get `create_item` with order number, customer, email, total, payment status, fulfillment status, shipping method, order date, flags and Shopify's update time. Existing orders get `change_multiple_column_values` on the same item.
- One subitem per line item with product name, SKU and quantity. An update adds any line items that are still missing.
- Late updates skipped: an update older than the Shopify update time on the board is ignored.
- `Idempotency-Key` on every `create_item` and `create_subitem`, so two deliveries racing each other, or a retry after a lost answer, never create a second item or line.
- VIP flag (`VIP_ORDER_TOTAL`) and rush flag (`RUSH_SHIPPING_KEYWORDS`, whole words). A Slack alert with the item link goes out once per flag, on the new order or on the update that adds the flag.
- A Slack note when the board already holds several items with the same order number.
- A small GraphQL client in each "Monday:" Code node: GraphQL variables, every GraphQL error (including errors inside HTTP 200 answers) fails the step, rate limits wait `retry_in_seconds`, timeouts and server errors are retried, bad tokens and bad requests are not.
- Configurable column IDs (`MONDAY_COLUMNS`, `MONDAY_SUBITEM_COLUMNS`), group, API version and retry limits, all checked on every run with clear error messages.
- Error messages made colon free, because n8n keeps only the text after the last colon of a Code node error.
- Sticky notes on the canvas, the error alert workflow and an end to end mock test.
