"""End to end test for the Shopify orders to monday.com fulfillment board workflow, against MOCK APIs.

No real Shopify store, monday.com account or Slack workspace is used. The script:
  1. starts a mock monday.com GraphQL API and a mock Slack incoming webhook on this machine,
  2. imports temporary copies of the workflow and the error alert workflow into your n8n, all named
     "TEST shopify-orders-to-monday (temporary)" (API hosts pointed at the mock, fake keys), plus one copy where
     SHOPIFY_WEBHOOK_SECRET is not set at all, one where it is only spaces and one with a config typo,
  3. sends signed Shopify webhooks exactly like Shopify does (X-Shopify-Hmac-Sha256, base64 HMAC SHA256 of the raw body)
     and plays the scenarios: forged and tampered webhooks, a missing secret, new orders, repeat deliveries, updates,
     late updates, VIP and rush flags (including the exact VIP limit), Slack markup typed by a shopper, duplicate items,
     two deliveries racing each other, rate limits, a locked board, a lost response, GraphQL errors, a line item that
     fails halfway, monday and Slack outages, a config typo and odd orders,
  4. checks every request the workflow made and the node each run ended on, prints PASS or FAIL per check,
     then deletes the temporary copies.

The mock monday API is stateful (a real board with items and subitems) and strict. Every request is parsed and
validated against a copy of the parts of monday's GraphQL schema the workflow uses, so an unknown field, a wrong
argument, an undeclared variable or a malformed query fails just like it would on monday. Column values must use
monday's JSON formats, status and dropdown labels must exist unless create_labels_if_missing is set, the token
and API-Version header are required, and mutations honour the Idempotency-Key header (409 while in flight,
the saved answer with Idempotency-Replayed: true afterwards).

Requirements: Python 3.9+, graphql-core (pip install graphql-core), a running n8n with the public API enabled.
Environment:
  N8N_URL      default http://localhost:5678
  N8N_API_KEY  your n8n API key (Settings > n8n API)
  MOCK_HOST    how n8n reaches this machine; default host.docker.internal (n8n in Docker).
               Use 127.0.0.1 if n8n runs directly on this machine.
Usage: python tests/e2e_mock_test.py
"""
import base64, hashlib, hmac, json, os, re, sys, threading, time, traceback, uuid, urllib.error, urllib.request
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

try:
    from graphql import GraphQLError, build_schema, execute_sync, parse, validate
    from graphql.error import GraphQLSyntaxError
    from graphql.language import OperationType, StringValueNode
except ImportError:
    raise SystemExit('This test needs graphql-core: pip install graphql-core')

N8N = os.environ.get('N8N_URL', 'http://localhost:5678').rstrip('/')
KEY = os.environ.get('N8N_API_KEY', '')
MOCK_PORT = 8781
MOCK = f"http://{os.environ.get('MOCK_HOST', 'host.docker.internal')}:{MOCK_PORT}"
ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / 'workflow' / 'shopify-orders-to-monday.json'
ERROR_WORKFLOW = ROOT / 'workflow' / 'error-alert-slack.json'
PATH = 'shopify-orders-to-monday-e2e'
TEMP_NAME = 'TEST shopify-orders-to-monday (temporary)'
SECRET = 'test-shopify-secret'
SHOP = 'northwind-goods-test.myshopify.com'
TOKEN = 'mock-monday-token'
BOARD_ID, SUBITEM_BOARD_ID, GROUP_ID = '5000000001', '5000000002', 'new_orders'
COLUMNS = {'order_number': 'text_mkx1ordr', 'customer': 'text_mkx1cust', 'email': 'email_mkx1mail', 'total': 'numeric_mkx1totl',
           'payment_status': 'color_mkx1paym', 'fulfillment_status': 'color_mkx1fulf', 'shipping_method': 'text_mkx1ship',
           'order_date': 'date_mkx1ordd', 'flags': 'dropdown_mkx1flag', 'shopify_updated_at': 'text_mkx1upd8'}
SUBITEM_COLUMNS = {'sku': 'text_mkx2sku0', 'quantity': 'numeric_mkx2qty0'}
ENV = {
    'SHOPIFY_WEBHOOK_SECRET': SECRET,
    'MONDAY_API_TOKEN': TOKEN, 'MONDAY_API_VERSION': '2026-07', 'MONDAY_BOARD_ID': BOARD_ID, 'MONDAY_GROUP_ID': GROUP_ID,
    'MONDAY_COLUMNS': json.dumps(COLUMNS), 'MONDAY_SUBITEM_COLUMNS': json.dumps(SUBITEM_COLUMNS),
    'MONDAY_MAX_ATTEMPTS': '3', 'MONDAY_MAX_WAIT_SECONDS': '2',
    'VIP_ORDER_TOTAL': '500', 'RUSH_SHIPPING_KEYWORDS': 'express,overnight,next day,priority',
    'SLACK_WEBHOOK_URL': MOCK + '/slack/hook',
}
HOSTS = {'https://api.monday.com': MOCK}

# --------------------------------------------------------------------------------------------------
# Mock monday.com: the parts of the real GraphQL schema the workflow uses (ColumnValue is an interface
# in the real schema; a plain type is enough for the id and text fields read here).
# --------------------------------------------------------------------------------------------------
SDL = '''
scalar JSON
scalar Date
enum State { active all archived deleted }
enum PositionRelative { before_at after_at }
enum ColumnType { text long_text numbers email status date dropdown name }
type Board { id: ID! name: String! }
type Group { id: ID! title: String! }
type ColumnValue { id: ID! text: String value: JSON type: ColumnType! }
type Item {
  id: ID!
  name: String!
  url: String!
  state: State
  created_at: Date
  updated_at: Date
  board: Board
  group: Group
  parent_item: Item
  column_values(ids: [String!], types: [ColumnType!]): [ColumnValue!]!
  subitems: [Item]
}
type ItemsResponse { cursor: String items: [Item!]! }
input ItemsPageByColumnValuesQuery { column_id: String! column_values: [String]! }
type Query {
  items(ids: [ID!], limit: Int = 25, page: Int = 1): [Item]
  items_page_by_column_values(board_id: ID!, columns: [ItemsPageByColumnValuesQuery!], cursor: String, limit: Int! = 25): ItemsResponse!
}
type Mutation {
  create_item(board_id: ID!, item_name: String!, column_values: JSON, group_id: String, create_labels_if_missing: Boolean,
              position_relative_method: PositionRelative, relative_to: ID): Item
  create_subitem(parent_item_id: ID!, item_name: String!, column_values: JSON, create_labels_if_missing: Boolean): Item
  change_multiple_column_values(item_id: ID, board_id: ID!, column_values: JSON!, create_labels_if_missing: Boolean): Item
}
'''
SCHEMA = build_schema(SDL)


def _json_value(value):
    if not isinstance(value, str):
        raise TypeError('column values must be sent as a JSON string')
    return json.loads(value)


def _json_literal(node, _variables=None):
    if isinstance(node, StringValueNode):
        return json.loads(node.value)
    raise TypeError('a JSON literal must be a string')


# graphql-core 3.2 reads parse_value and friends, 3.3 reads the coerce_* copies, so set both.
JSON_TYPE = SCHEMA.type_map['JSON']
JSON_TYPE.parse_value = JSON_TYPE.coerce_input_value = _json_value
JSON_TYPE.parse_literal = _json_literal
JSON_TYPE.coerce_input_literal = lambda node, *_: _json_literal(node)
JSON_TYPE.serialize = JSON_TYPE.coerce_output_value = lambda v: v

BOARDS = {
    BOARD_ID: {'name': 'Fulfillment', 'groups': {'topics': 'Orders', GROUP_ID: 'New orders'},
               'columns': {'name': 'name', COLUMNS['order_number']: 'text', COLUMNS['customer']: 'text', COLUMNS['email']: 'email',
                           COLUMNS['total']: 'numbers', COLUMNS['payment_status']: 'status', COLUMNS['fulfillment_status']: 'status',
                           COLUMNS['shipping_method']: 'text', COLUMNS['order_date']: 'date', COLUMNS['flags']: 'dropdown',
                           COLUMNS['shopify_updated_at']: 'text'}},
    SUBITEM_BOARD_ID: {'name': 'Subitems of Fulfillment', 'groups': {'topics': 'Subitems'},
                       'columns': {'name': 'name', SUBITEM_COLUMNS['sku']: 'text', SUBITEM_COLUMNS['quantity']: 'numbers'}},
}
LABELS = {COLUMNS['payment_status']: {'Paid', 'Pending'}, COLUMNS['fulfillment_status']: {'Unfulfilled', 'Fulfilled'}, COLUMNS['flags']: set()}
ITEMS = {}                    # item id -> {id, name, board, group, values, parent, subitems}
NEXT_ID = [9100000001]
LOCK = threading.Lock()
IDEMPOTENCY = {'cache': {}, 'in_flight': set()}
FAULT = {'down': False, 'unauthorized': False, 'rate_limit': 0, 'locked': 0, 'lost_create': False, 'create_error': False,
         'subitem_error_in': 0, 'create_delay': 0.0, 'slack_down': False}
LOG = []                      # every request the mocks received, in order


def app_error(code, message, **error_data):
    return GraphQLError(message, extensions={'code': code, 'status_code': 200, 'error_data': error_data})


def coerce(board_id, column, ctype, value, create_labels):
    """Checks one column value the way monday does and returns what is stored (None clears the column)."""
    bad = lambda why: app_error('ColumnValueException', f'invalid value, please check our API documentation for the correct data structure for this column ({why})',
                                column_id=column, column_type=ctype, column_value=json.dumps(value))
    if value is None:
        return None
    if ctype == 'text':
        if not isinstance(value, str):
            raise bad('text columns take a plain string')
        return value
    if ctype == 'email':
        if not isinstance(value, dict) or not re.fullmatch(r'[^@\s]+@[^@\s]+\.[a-z]{2,}', str(value.get('email', '')), re.I) or not isinstance(value.get('text'), str):
            raise bad('email columns take {"email": ..., "text": ...}')
        return {'email': value['email'], 'text': value['text']}
    if ctype == 'numbers':
        if not isinstance(value, str) or not re.fullmatch(r'-?\d+(\.\d+)?', value):
            raise bad('numbers columns take a number as a string')
        return value
    if ctype == 'status':
        if value == {}:
            return None
        label = value.get('label') if isinstance(value, dict) else None
        if not isinstance(label, str) or not label:
            raise bad('status columns take {"label": ...}')
        if label not in LABELS[column]:
            if not create_labels:
                raise bad(f'status label "{label}" does not exist')
            LABELS[column].add(label)
        return label
    if ctype == 'date':
        if not isinstance(value, dict) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', str(value.get('date', ''))):
            raise bad('date columns take {"date": "YYYY-MM-DD", "time": "HH:MM:SS"}')
        date.fromisoformat(value['date'])
        if 'time' in value and not re.fullmatch(r'([01]\d|2[0-3]):[0-5]\d:[0-5]\d', str(value['time'])):
            raise bad('time must be HH:MM:SS')
        return {'date': value['date'], **({'time': value['time']} if 'time' in value else {})}
    if ctype == 'dropdown':
        labels = value.get('labels') if isinstance(value, dict) else None
        if not isinstance(labels, list) or not all(isinstance(x, str) and x for x in labels):
            raise bad('dropdown columns take {"labels": [...]}')
        for label in labels:
            if label not in LABELS[column]:
                if not create_labels:
                    raise bad(f'dropdown label "{label}" does not exist')
                LABELS[column].add(label)
        return list(labels) or None
    raise bad(f'column type {ctype} cannot be written here')


def write(board_id, item, values, create_labels):
    if values is None:
        return
    if not isinstance(values, dict):
        raise app_error('ColumnValueException', 'column_values must be a JSON object')
    columns = BOARDS[board_id]['columns']
    clean = {}
    for column, value in values.items():
        if column not in columns or column == 'name':
            raise app_error('InvalidColumnIdException', "This column ID doesn't exist for the board", column_id=column, board_id=board_id)
        clean[column] = coerce(board_id, column, columns[column], value, create_labels)
    item['values'].update(clean)


def text_of(board_id, item, column):
    value, ctype = item['values'].get(column), BOARDS[board_id]['columns'].get(column)
    if value is None:
        return ''
    if ctype == 'email':
        return value['text'] or value['email']
    if ctype == 'date':
        return value['date'] + (' ' + value['time'][:5] if 'time' in value else '')
    if ctype == 'dropdown':
        return ', '.join(value)
    return str(value)


def view(item):
    board_id = item['board']

    def column_values(info, ids=None, types=None):
        return [{'id': c, 'text': text_of(board_id, item, c), 'value': item['values'].get(c), 'type': t}
                for c, t in BOARDS[board_id]['columns'].items() if c != 'name' and (ids is None or c in ids)]
    return {'id': item['id'], 'name': item['name'], 'state': 'active',
            'url': f"https://northwind-goods-test.monday.com/boards/{board_id}/pulses/{item['id']}",
            'board': {'id': board_id, 'name': BOARDS[board_id]['name']},
            'group': {'id': item['group'], 'title': BOARDS[board_id]['groups'][item['group']]},
            'parent_item': view(ITEMS[item['parent']]) if item['parent'] else None,
            'column_values': column_values, 'subitems': lambda info: [view(ITEMS[s]) for s in item['subitems']]}


def new_item(board_id, name, group, parent=None):
    if not isinstance(name, str) or not 1 <= len(name) <= 255:
        raise app_error('ItemNameTooLongException', 'Item name must be between 1 and 255 characters long')
    item = {'id': str(NEXT_ID[0]), 'name': name, 'board': board_id, 'group': group, 'values': {}, 'parent': parent, 'subitems': []}
    NEXT_ID[0] += 1
    return item


class Root:
    def items(self, info, ids=None, limit=25, page=1):
        return [view(ITEMS[i]) for i in (ids or []) if i in ITEMS]

    def items_page_by_column_values(self, info, board_id, limit, columns=None, cursor=None):
        if board_id not in BOARDS:
            raise app_error('InvalidBoardIdException', 'Board does not exist', board_id=board_id)
        if not 1 <= limit <= 500:
            raise app_error('InvalidArgumentException', 'limit must be between 1 and 500')
        if not columns and not cursor:
            raise app_error('InvalidArgumentException', 'columns or cursor is required')
        for c in columns or []:
            if c['column_id'] not in BOARDS[board_id]['columns']:
                raise app_error('InvalidColumnIdException', "This column ID doesn't exist for the board", column_id=c['column_id'], board_id=board_id)
        found = [it for it in ITEMS.values() if it['board'] == board_id and it['parent'] is None and all(
            text_of(board_id, it, c['column_id']).lower() in [str(v).lower() for v in c['column_values']] for c in columns or [])]
        return {'cursor': None, 'items': [view(it) for it in sorted(found, key=lambda it: int(it['id']))[:limit]]}

    def create_item(self, info, board_id, item_name, column_values=None, group_id=None, create_labels_if_missing=None, **_):
        if board_id not in BOARDS or board_id == SUBITEM_BOARD_ID:
            raise app_error('InvalidBoardIdException', 'Board does not exist', board_id=board_id)
        if FAULT['create_error']:
            FAULT['create_error'] = False
            LOG[-1]['injected'] = True
            raise app_error('ColumnValueException', 'invalid value, please check our API documentation for the correct data structure for this column',
                            column_id=COLUMNS['payment_status'])
        group = group_id or 'topics'
        if group not in BOARDS[board_id]['groups']:
            raise app_error('InvalidArgumentException', f'Group {group} does not exist on the board')
        item = new_item(board_id, item_name, group)
        write(board_id, item, column_values, create_labels_if_missing)
        ITEMS[item['id']] = item
        return view(item)

    def create_subitem(self, info, parent_item_id, item_name, column_values=None, create_labels_if_missing=None):
        parent = ITEMS.get(parent_item_id)
        if not parent:
            raise app_error('ResourceNotFoundException', 'Item not found', item_id=parent_item_id)
        if FAULT['subitem_error_in']:
            FAULT['subitem_error_in'] -= 1
            if FAULT['subitem_error_in'] == 0:
                LOG[-1]['injected'] = True
                raise app_error('ColumnValueException', 'invalid value, please check our API documentation for the correct data structure for this column',
                                column_id=SUBITEM_COLUMNS['sku'])
        item = new_item(SUBITEM_BOARD_ID, item_name, 'topics', parent=parent_item_id)
        write(SUBITEM_BOARD_ID, item, column_values, create_labels_if_missing)
        ITEMS[item['id']] = item
        parent['subitems'].append(item['id'])
        return view(item)

    def change_multiple_column_values(self, info, board_id, column_values, item_id=None, create_labels_if_missing=None):
        item = ITEMS.get(item_id or '')
        if not item or item['board'] != board_id:
            raise app_error('ResourceNotFoundException', 'Item not found in board', item_id=item_id, board_id=board_id)
        write(board_id, item, column_values, create_labels_if_missing)
        return view(item)


ROOT_VALUE = Root()


class Mock(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *args):
        pass

    def _send(self, code, obj, extra=None):
        data = (obj if isinstance(obj, str) else json.dumps(obj)).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'text/plain' if isinstance(obj, str) else 'application/json')
        self.send_header('Content-Length', str(len(data)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self._send(404, 'not mocked')

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get('Content-Length') or 0))
        headers = {k.lower(): v for k, v in self.headers.items()}
        if self.path == '/slack/hook':
            return self.slack(raw, headers)
        if self.path == '/v2':
            return self.monday(raw, headers)
        self._send(404, {'error': 'not mocked'})

    def slack(self, raw, headers):
        entry = {'service': 'slack', 't': time.time(), 'text': None}
        LOG.append(entry)
        if not headers.get('content-type', '').startswith('application/json'):
            return self._send(400, 'invalid_payload')
        try:
            body = json.loads(raw)
        except ValueError:
            return self._send(400, 'invalid_payload')
        if not isinstance(body.get('text'), str) or not body['text'].strip():
            return self._send(400, 'no_text')
        entry['text'] = body['text']
        if FAULT['slack_down']:
            entry['status'] = 503
            return self._send(503, 'service_unavailable')
        entry['status'] = 200
        self._send(200, 'ok')

    def monday(self, raw, headers):
        e = {'service': 'monday', 't': time.time(), 'headers': headers, 'fields': [], 'variables': {}, 'key': headers.get('idempotency-key'),
             'status': None, 'executed': False, 'replayed': False, 'conflict': False, 'injected': False, 'rejected': None}
        LOG.append(e)

        def reply(code, obj, extra=None, rejected=None, injected=False):
            e.update(status=code, response=obj, rejected=rejected, injected=injected or e['injected'])
            self._send(code, obj, extra)

        if FAULT['unauthorized']:
            return reply(401, {'errors': [{'message': 'Unauthorized', 'extensions': {'code': 'Unauthorized', 'status_code': 401}}]}, injected=True)
        if headers.get('authorization') != TOKEN:
            return reply(401, {'errors': [{'message': 'Unauthorized', 'extensions': {'code': 'Unauthorized', 'status_code': 401}}]}, rejected='token')
        if not headers.get('content-type', '').startswith('application/json'):
            return reply(400, {'errors': [{'message': 'Bad request', 'extensions': {'code': 'BadRequest', 'status_code': 400}}]}, rejected='content type')
        try:
            body = json.loads(raw)
            query, variables = body['query'], body.get('variables') or {}
            assert isinstance(query, str) and isinstance(variables, dict)
        except (ValueError, KeyError, AssertionError, TypeError):
            return reply(400, {'errors': [{'message': 'JsonParseException', 'extensions': {'code': 'JsonParseException', 'status_code': 400}}]}, rejected='json')
        e.update(query=query, variables=variables)
        try:
            doc = parse(query)
        except GraphQLSyntaxError as err:
            return reply(200, {'errors': [{'message': f'Parse error on {err.message}', 'locations': [loc._asdict() for loc in err.locations or []]}],
                               'account_id': 4242}, rejected='parse')
        problems = validate(SCHEMA, doc)
        if problems:
            return reply(200, {'errors': [p.formatted for p in problems], 'account_id': 4242}, rejected='schema')
        ops = [d for d in doc.definitions if hasattr(d, 'operation')]
        e['op'] = ops[0].operation.value
        e['fields'] = [s.name.value for s in ops[0].selection_set.selections]
        if not re.fullmatch(r'\d{4}-\d{2}', headers.get('api-version', '')):
            return reply(200, {'errors': [{'message': 'Invalid API version', 'extensions': {'code': 'InvalidVersionException', 'status_code': 200}}]}, rejected='api version')
        if FAULT['down']:
            return reply(500, {'errors': [{'message': 'Internal server error', 'extensions': {'code': 'INTERNAL_SERVER_ERROR', 'status_code': 500}}]}, injected=True)
        if FAULT['rate_limit']:
            FAULT['rate_limit'] -= 1
            return reply(429, {'errors': [{'message': 'Complexity budget exhausted', 'extensions': {
                'code': 'COMPLEXITY_BUDGET_EXHAUSTED', 'complexity': 30010, 'complexity_budget_left': 100,
                'complexity_budget_limit': 10000000, 'retry_in_seconds': 1, 'status_code': 429}}], 'account_id': 4242}, injected=True)
        if FAULT['locked'] and 'change_multiple_column_values' in e['fields']:
            # monday's documented 423 when another write holds the board; it has no error code, so only the status tells
            FAULT['locked'] -= 1
            return reply(423, {'errors': [{'message': 'Resource is currently locked, please try again later', 'extensions': {'status_code': 423}}],
                               'account_id': 4242}, {'Retry-After': '1'}, injected=True)

        key = e['key'] if ops[0].operation == OperationType.MUTATION else None
        if key:
            with LOCK:
                if key in IDEMPOTENCY['in_flight']:
                    e['conflict'] = True
                    return reply(409, {'errors': [{'message': 'A request with this idempotency key is currently being processed',
                                                   'extensions': {'code': 'IDEMPOTENCY_CONFLICT'}}]}, {'Retry-After': '2'})
                if key in IDEMPOTENCY['cache']:
                    e['replayed'] = True
                    return reply(200, IDEMPOTENCY['cache'][key], {'Idempotency-Replayed': 'true'})
                IDEMPOTENCY['in_flight'].add(key)
        try:
            if 'create_item' in e['fields'] and FAULT['create_delay']:
                time.sleep(FAULT['create_delay'])
            with LOCK:
                result = execute_sync(SCHEMA, doc, root_value=ROOT_VALUE, variable_values=variables)
                out = {'data': result.data, 'account_id': 4242}
                if result.errors:
                    out['errors'] = [err.formatted for err in result.errors]
                e['executed'] = bool(result.data and any(result.data.values()))
                if key and not result.errors:
                    IDEMPOTENCY['cache'][key] = out
            if result.errors and not e['injected']:
                return reply(200, out, rejected='graphql: ' + '; '.join(err.message for err in result.errors))
            if 'create_item' in e['fields'] and FAULT['lost_create']:
                FAULT['lost_create'] = False          # monday did the work, but the answer never reached n8n
                return reply(504, 'Gateway Time-out', injected=True)
            return reply(200, out)
        finally:
            if key:
                with LOCK:
                    IDEMPOTENCY['in_flight'].discard(key)


# --------------------------------------------------------------------------------------------------
# Fake Shopify orders (made-up store, customers and products)
# --------------------------------------------------------------------------------------------------
PRODUCTS = {
    'tote': ('Canvas Tote Bag', 'Natural', 'NWG-TOTE-NAT', '24.95'),
    'mug': ('Stoneware Mug', 'Slate', 'NWG-MUG-SLT', '18.00'),
    'candle': ('Soy Candle', 'Cedar', 'NWG-CNDL-CDR', '22.50'),
    'throw': ('Wool Throw Blanket', 'Oatmeal', 'NWG-THRW-OAT', '189.00'),
    'lamp': ('Brass Desk Lamp', '', 'NWG-LAMP-BRS', '149.00'),
    'rug': ('Jute Area Rug', 'Large', 'NWG-RUG-JUT-L', '495.00'),        # with Standard Shipping: exactly 500.00
    'rug_sale': ('Jute Area Rug', 'Large (sale)', 'NWG-RUG-JUT-LS', '494.99'),  # with Standard Shipping: 499.99
}
SHIPPING_PRICES = {'Standard Shipping': '5.00', 'Express Overnight': '24.00', 'Expressway Freight Ground': '9.00', 'USPS PRIORITY MAIL': '11.00'}


def order(num, first='Jamie', last='Rivera', lines=(('tote', 2), ('mug', 1)), shipping='Standard Shipping', financial='paid',
          fulfillment=None, created='2026-10-20T09:15:02-04:00', updated=None, cancelled=None, guest=False, email=None):
    oid = 7100000000000 + num
    email = email or f'{first}.{last}'.lower().replace('é', 'e').replace('í', 'i') + '@example.com'
    items, subtotal = [], 0.0
    for n, (key, qty) in enumerate(lines):
        title, variant, sku, price = PRODUCTS[key]
        lid = 15100000000000 + num * 10 + n
        subtotal += float(price) * qty
        items.append({'id': lid, 'admin_graphql_api_id': f'gid://shopify/LineItem/{lid}', 'title': title, 'variant_title': variant or None,
                      'name': f'{title} - {variant}' if variant else title, 'sku': sku, 'quantity': qty, 'current_quantity': qty,
                      'fulfillable_quantity': qty, 'price': price, 'vendor': 'Northwind Goods', 'product_id': 8800000000000 + n,
                      'variant_id': 4600000000000 + n, 'requires_shipping': True, 'taxable': True, 'fulfillment_status': None})
    ship = [] if shipping is None else [{'id': 5300000000000 + num, 'title': shipping, 'code': shipping, 'price': SHIPPING_PRICES[shipping],
                                         'source': 'shopify', 'carrier_identifier': None, 'discounted_price': SHIPPING_PRICES[shipping]}]
    total = f'{subtotal + sum(float(s["price"]) for s in ship):.2f}'
    address = {'first_name': first, 'last_name': last, 'name': f'{first} {last}', 'address1': '100 Example Street', 'city': 'Springfield',
               'province_code': 'OR', 'zip': '97477', 'country_code': 'US', 'phone': '+15555550100'}
    return {
        'id': oid, 'admin_graphql_api_id': f'gid://shopify/Order/{oid}', 'name': f'#{num}', 'order_number': num, 'number': num - 1000,
        'email': email, 'contact_email': email, 'phone': None, 'created_at': created, 'processed_at': created, 'updated_at': updated or created,
        'cancelled_at': cancelled, 'cancel_reason': 'customer' if cancelled else None, 'closed_at': None, 'test': False,
        'currency': 'USD', 'presentment_currency': 'USD', 'subtotal_price': f'{subtotal:.2f}', 'total_tax': '0.00', 'total_price': total,
        'current_total_price': '0.00' if cancelled else total, 'financial_status': financial, 'fulfillment_status': fulfillment,
        'tags': '', 'note': None, 'source_name': 'web',
        'customer': None if guest else {'id': 6200000000000 + num, 'email': email, 'first_name': first, 'last_name': last, 'state': 'enabled'},
        'billing_address': address, 'shipping_address': None if shipping is None else address,
        'line_items': items, 'shipping_lines': ship,
    }


# --------------------------------------------------------------------------------------------------
# n8n helpers
# --------------------------------------------------------------------------------------------------
def api(method, path, body=None):
    req = urllib.request.Request(N8N + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={'X-N8N-API-KEY': KEY, 'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=60) as r:
        text = r.read().decode()
        return json.loads(text) if text else {}


def test_copy(path, env, webhook_id=None):
    """The workflow with $env values replaced by test values and the monday host pointed at the mock.
    A variable set to None stays a real $env lookup of a name that is never set, so n8n sees undefined,
    exactly like a variable that was never added to the n8n environment."""
    wf = json.loads((WORKFLOW if path else ERROR_WORKFLOW).read_text(encoding='utf-8'))

    def env_value(name):
        value = env.get(name, '')
        return f'$env.E2E_NEVER_SET_{name}' if value is None else "'" + value + "'"

    def fix(value):
        if isinstance(value, dict):
            return {k: fix(v) for k, v in value.items()}
        if isinstance(value, list):
            return [fix(v) for v in value]
        if not isinstance(value, str):
            return value
        value = re.sub(r'\$env\.([A-Z0-9_]+)', lambda m: env_value(m.group(1)), value)
        for real, mock in HOSTS.items():
            value = value.replace(real, mock)
        return value

    for node in wf['nodes']:
        node['parameters'] = fix(node.get('parameters', {}))
        if node['type'] == 'n8n-nodes-base.webhook':
            node['parameters']['path'] = path
            node['webhookId'] = webhook_id or node.get('webhookId')
    return wf


def import_workflow(name, wf, settings=None):
    return api('POST', '/api/v1/workflows', {'name': name, 'nodes': wf['nodes'], 'connections': wf['connections'],
                                             'settings': {**wf.get('settings', {}), **(settings or {})}})['id']


class Runner:
    def __init__(self, wid, path):
        self.wid, self.path, self.sent, self.mark, self.results = wid, path, 0, 0, []

    def wait(self, timeout=90):
        end = time.time() + timeout
        while time.time() < end:
            done = [e for e in api('GET', f'/api/v1/executions?workflowId={self.wid}&limit=250').get('data', []) if e.get('stoppedAt')]
            if len(done) >= self.sent:
                return sorted(done, key=lambda e: int(e['id']))
            time.sleep(0.4)
        raise TimeoutError('the workflow did not finish in time')

    def post(self, topic, payload, webhook_id=None, sign='valid', pretty=False, tamper=None):
        """Sends one webhook the way Shopify does and returns (status, body, milliseconds). Does not wait for the run."""
        data = json.dumps(payload, ensure_ascii=False, indent=2 if pretty else None).encode('utf-8')
        headers = {'Content-Type': 'application/json', 'X-Shopify-Topic': topic, 'X-Shopify-Shop-Domain': SHOP,
                   'X-Shopify-API-Version': '2026-10', 'X-Shopify-Webhook-Id': webhook_id or str(uuid.uuid4()),
                   'X-Shopify-Event-Id': str(uuid.uuid4()), 'X-Shopify-Triggered-At': time.strftime('%Y-%m-%dT%H:%M:%S.000000Z', time.gmtime())}
        key = {'valid': SECRET, 'empty-key': ''}.get(sign)
        if key is not None:
            headers['X-Shopify-Hmac-Sha256'] = base64.b64encode(hmac.new(key.encode(), data, hashlib.sha256).digest()).decode()
        if sign == 'wrong':
            headers['X-Shopify-Hmac-Sha256'] = base64.b64encode(hashlib.sha256(b'not the secret').digest()).decode()
        if tamper:
            data = tamper(data)
        t0 = time.time()
        try:
            with urllib.request.urlopen(urllib.request.Request(f'{N8N}/webhook/{self.path}', method='POST', data=data, headers=headers), timeout=30) as r:
                status, text = r.status, r.read().decode()
        except urllib.error.HTTPError as e:
            status, text = e.code, e.read().decode()
        self.sent += 1
        return status, text, int((time.time() - t0) * 1000)

    def send(self, topic, payload, **kwargs):
        """Sends one webhook, waits for the run, returns (status, body, ms, execution)."""
        self.mark = len(LOG)
        status, text, ms = self.post(topic, payload, **kwargs)
        return status, text, ms, self.wait()[-1]

    def stopped_at(self, ex):
        data = api('GET', f"/api/v1/executions/{ex['id']}?includeData=true").get('data') or {}
        result = data.get('resultData') or {}
        return result.get('lastNodeExecuted'), (result.get('error') or {}).get('message', '')

    def monday(self, field=None):
        return [e for e in LOG[self.mark:] if e['service'] == 'monday' and (field is None or field in e['fields'])]

    def alerts(self):
        return [e['text'] for e in LOG[self.mark:] if e['service'] == 'slack' and e['text'] and not e['text'].startswith(':rotating_light:')
                and e.get('status') == 200]

    def error_alert(self, ex, timeout=30):
        """Waits for the error workflow's Slack alert about this run (the alert links to the run's execution)."""
        end = time.time() + timeout
        while time.time() < end:
            hits = [e['text'] for e in LOG if e['service'] == 'slack' and (e['text'] or '').startswith(':rotating_light:')
                    and (e['text'] or '').rstrip().endswith(f"/executions/{ex['id']}")]
            if hits:
                return hits[0]
            time.sleep(0.4)
        return ''

    def check(self, scenario, label, ok, detail=''):
        self.results.append((scenario, label, bool(ok), detail))


def items_for(name):
    return [it for it in ITEMS.values() if it['board'] == BOARD_ID and it['values'].get(COLUMNS['order_number']) == name]


def subitems(item):
    return [(ITEMS[s]['name'], ITEMS[s]['values'].get(SUBITEM_COLUMNS['sku']), ITEMS[s]['values'].get(SUBITEM_COLUMNS['quantity'])) for s in item['subitems']]


def val(item, key):
    return item['values'].get(COLUMNS[key])


def url(item):
    return f"https://northwind-goods-test.monday.com/boards/{BOARD_ID}/pulses/{item['id']}"


SAMPLES = {}


# --------------------------------------------------------------------------------------------------
# Scenarios
# --------------------------------------------------------------------------------------------------
def run(r, v, c, b):
    # 1. Forged and tampered webhooks
    for label, kwargs in (('wrong signature', {'sign': 'wrong'}), ('no signature', {'sign': 'none'}),
                          ('body changed after signing', {'tamper': lambda d: d.replace(b'"total_price": "72.90"', b'"total_price": "0.01"')})):
        status, text, _, ex = r.send('orders/create', order(1001), **kwargs)
        node, _ = r.stopped_at(ex)
        r.check(f'Forged webhook, {label}', 'refused with 401, nothing sent to monday',
                status == 401 and text == 'Invalid webhook signature' and node == 'Stop: Bad Signature' and not r.monday() and not r.alerts(),
                f'{status}, ended at {node}')
        alert = r.error_alert(ex)
        r.check(f'Forged webhook, {label}', 'error workflow posts the alert to Slack',
                'failed at *Stop: Bad Signature*' in alert and 'X-Shopify-Hmac-Sha256 header is missing or wrong' in alert
                and f'(topic orders/create, shop {SHOP})' in alert, alert.splitlines()[0] if alert else 'no alert')

    # The topic header of an unsigned request is attacker controlled; it must not reach Slack as a clickable link
    status, _, _, ex = r.send('<https://phish.example.com|Reset your password>', order(1001), sign='wrong')
    alert = r.error_alert(ex)
    r.check('Forged webhook with a Slack link in its topic header', 'refused, and the header text never reaches Slack',
            status == 401 and 'topic not given' in alert and 'phish.example.com' not in alert,
            f'{status}, the alert says "topic not given"' if 'topic not given' in alert else repr(alert))

    # SHOPIFY_WEBHOOK_SECRET not set at all. Before the review fix, the Crypto node threw on the undefined key and
    # n8n answered 200 with an empty body, so Shopify counted every order as delivered.
    outcomes = []
    for sign in ('empty-key', 'none', 'valid'):
        status, text, _, ex = v.send('orders/create', order(1030), sign=sign)
        node, _ = v.stopped_at(ex)
        outcomes.append((sign, status, text, node, bool(v.monday()), v.error_alert(ex)))
    v.check('Webhook secret not set (variable missing)', 'fails closed: 401 for an empty key signature, no signature and the real signature',
            all(s == 401 and t == 'Invalid webhook signature' and n == 'Stop: Bad Signature' and not m for _, s, t, n, m, _ in outcomes),
            ', '.join(f'{sign} {s} at {n}' for sign, s, _, n, _, _ in outcomes))
    v.check('Webhook secret not set (variable missing)', 'each alert says the secret is not set',
            all('SHOPIFY_WEBHOOK_SECRET is not set in n8n, so every webhook is refused' in a for *_, a in outcomes),
            (outcomes[0][5].splitlines()[1][:90] if outcomes[0][5].count('\n') else repr(outcomes[0][5])))
    r.results += v.results

    # A secret of only spaces counts as not set. The signing step and the check both trim it, so a request
    # signed with an empty key cannot slip through.
    status, text, _, ex = b.send('orders/create', order(1031), sign='empty-key')
    node, _ = b.stopped_at(ex)
    alert = b.error_alert(ex)
    b.check('Webhook secret of only spaces', 'treated as not set: 401 for a request signed with an empty key',
            status == 401 and node == 'Stop: Bad Signature' and not b.monday() and 'SHOPIFY_WEBHOOK_SECRET is not set' in alert,
            f'{status}, ended at {node}')
    r.results += b.results

    # 2. A new order
    first_id = str(uuid.uuid4())
    status, text, ms, ex = r.send('orders/create', order(1001), webhook_id=first_id)
    finds, creates, subs = r.monday('items_page_by_column_values'), r.monday('create_item'), r.monday('create_subitem')
    item = (items_for('#1001') or [None])[0]
    r.check('New order #1001', 'Shopify gets 200 OK in under a second (its limit is 5 seconds)', status == 200 and text == 'OK' and ms < 1000,
            f'{status} {text!r} in {ms} ms')
    r.check('New order #1001', 'looked up by order number, then one item created',
            len(finds) == 1 and finds[0]['variables'].get('column') == COLUMNS['order_number'] and finds[0]['variables'].get('value') == '#1001'
            and finds[0]['variables'].get('board') == BOARD_ID and len(creates) == 1 and len(items_for('#1001')) == 1, f'{len(finds)} search, {len(creates)} create')
    expected = {COLUMNS['order_number']: '#1001', COLUMNS['customer']: 'Jamie Rivera',
                COLUMNS['email']: {'email': 'jamie.rivera@example.com', 'text': 'jamie.rivera@example.com'},
                COLUMNS['total']: '72.90', COLUMNS['payment_status']: {'label': 'Paid'}, COLUMNS['fulfillment_status']: {'label': 'Unfulfilled'},
                COLUMNS['shipping_method']: 'Standard Shipping', COLUMNS['order_date']: {'date': '2026-10-20', 'time': '13:15:02'},
                COLUMNS['shopify_updated_at']: '2026-10-20T13:15:02Z'}
    cv = creates[0]['variables'] if creates else {}
    sent_values = json.loads(cv.get('values', '{}'))
    r.check('New order #1001', 'item name, group and every column value exactly as mapped',
            cv.get('name') == '#1001 Jamie Rivera' and cv.get('group') == GROUP_ID and sent_values == expected
            and 'create_labels_if_missing: true' in creates[0]['query'], json.dumps(sent_values) if sent_values != expected else 'all 9 columns match')
    hdr = [e['headers'] for e in r.monday()]
    r.check('New order #1001', 'monday headers: token, API-Version 2026-07, JSON, Idempotency-Key on writes only',
            all(h.get('authorization') == TOKEN and h.get('api-version') == '2026-07' and h.get('content-type', '').startswith('application/json') for h in hdr)
            and 'idempotency-key' not in finds[0]['headers'] and creates[0]['key'] == f'shopify-order:{BOARD_ID}:7100000001001'
            and len({s['key'] for s in subs}) == len(subs) == 2, f'{len(hdr)} requests checked')
    r.check('New order #1001', 'one subitem per line item with name, SKU and quantity',
            item is not None and subitems(item) == [('Canvas Tote Bag - Natural', 'NWG-TOTE-NAT', '2'), ('Stoneware Mug - Slate', 'NWG-MUG-SLT', '1')]
            and all(s['variables'].get('parent') == item['id'] for s in subs), json.dumps(subitems(item)) if item else 'no item')
    r.check('New order #1001', 'not flagged, so no Slack alert', not r.alerts(), f'{len(r.alerts())} alerts')
    SAMPLES['find_query'] = finds[0].get('query', '') if finds else ''
    SAMPLES['find_variables'] = finds[0]['variables'] if finds else {}
    SAMPLES['create_item'] = cv
    SAMPLES['create_subitem'] = subs[0]['variables'] if subs else {}

    # 3. Shopify delivers the same webhook again
    status, _, _, ex = r.send('orders/create', order(1001), webhook_id=first_id)
    node, _ = r.stopped_at(ex)
    r.check('Same delivery again (Shopify retry)', 'skipped: 200, no monday calls',
            status == 200 and not r.monday() and node == 'Skip: Repeat or Other Topic', f'ended at {node}')

    # 4. The order is fulfilled
    status, _, _, ex = r.send('orders/updated', order(1001, fulfillment='fulfilled', updated='2026-10-20T15:40:11-04:00'))
    updates = r.monday('change_multiple_column_values')
    uv = json.loads(updates[0]['variables']['values']) if updates else {}
    item = items_for('#1001')[0]
    r.check('Order #1001 fulfilled (orders/updated)', 'the same item is updated, nothing new created',
            len(updates) == 1 and updates[0]['variables'].get('item') == item['id'] and not r.monday('create_item') and not r.monday('create_subitem')
            and len(items_for('#1001')) == 1 and len(item['subitems']) == 2, f"{len(updates)} update, {len(r.monday('create_item'))} creates")
    r.check('Order #1001 fulfilled (orders/updated)', 'fulfillment and Shopify update time changed on the board',
            uv.get(COLUMNS['fulfillment_status']) == {'label': 'Fulfilled'} and val(item, 'fulfillment_status') == 'Fulfilled'
            and val(item, 'shopify_updated_at') == '2026-10-20T19:40:11Z' and uv.get(COLUMNS['flags'], 'missing') is None,
            f"fulfillment {val(item, 'fulfillment_status')}")
    SAMPLES['update'] = updates[0]['variables'] if updates else {}

    # 5. An older update arrives after the newer one
    status, _, _, ex = r.send('orders/updated', order(1001, updated='2026-10-20T10:02:45-04:00'))
    node, _ = r.stopped_at(ex)
    r.check('Older update arrives late', 'skipped, the board keeps the newer state',
            node == 'Skip: Older Update' and not r.monday('change_multiple_column_values') and val(items_for('#1001')[0], 'fulfillment_status') == 'Fulfilled',
            f'ended at {node}')

    # 6. VIP and rush flags
    r.send('orders/create', order(1002, 'Taylor', 'Brooks', lines=(('throw', 2), ('lamp', 1), ('candle', 4))))
    item, alerts = items_for('#1002')[0], r.alerts()
    r.check('VIP order #1002 (total 622.00)', 'flagged VIP on the board and in Slack with a link to the item',
            val(item, 'flags') == ['VIP'] and len(alerts) == 1 and alerts[0].startswith('*VIP order #1002*') and f'Open in monday.com: {url(item)}' in alerts[0]
            and 'Total: 622.00 USD' in alerts[0], alerts[0].splitlines()[0] if alerts else 'no alert')
    SAMPLES['vip_alert'] = alerts[0] if alerts else ''
    r.send('orders/create', order(1003, 'Sam', 'Cole', lines=(('candle', 1),), shipping='Express Overnight'))
    item, alerts = items_for('#1003')[0], r.alerts()
    r.check('Rush order #1003 (Express Overnight)', 'flagged Rush, Slack says why',
            val(item, 'flags') == ['Rush'] and len(alerts) == 1 and alerts[0].startswith('*Rush order #1003*')
            and 'shipping "Express Overnight" matches a rush keyword' in alerts[0], alerts[0].splitlines()[0] if alerts else 'no alert')
    SAMPLES['rush_alert'] = alerts[0] if alerts else ''
    r.send('orders/create', order(1005, 'Morgan', 'Lee', lines=(('mug', 2),), shipping='Expressway Freight Ground'))
    r.check('Shipping "Expressway Freight Ground"', 'keywords match whole words only, so no rush flag',
            val(items_for('#1005')[0], 'flags') is None and not r.alerts(), f"flags {val(items_for('#1005')[0], 'flags')}")
    r.send('orders/updated', order(1002, 'Taylor', 'Brooks', lines=(('throw', 2), ('lamp', 1), ('candle', 4)), fulfillment='fulfilled',
                                   updated='2026-10-20T16:05:00-04:00'))
    r.check('VIP order #1002 fulfilled', 'no second alert for a flag already on the board',
            not r.alerts() and val(items_for('#1002')[0], 'fulfillment_status') == 'Fulfilled', f'{len(r.alerts())} alerts')
    r.send('orders/updated', order(1005, 'Morgan', 'Lee', lines=(('mug', 2),), shipping='Express Overnight', updated='2026-10-20T11:30:00-04:00'))
    alerts = r.alerts()
    r.check('Order #1005 upgraded to Express Overnight', 'one alert: the order is now Rush',
            len(alerts) == 1 and alerts[0].startswith('*Order #1005 is now Rush*') and val(items_for('#1005')[0], 'flags') == ['Rush'],
            alerts[0].splitlines()[0] if alerts else 'no alert')

    # VIP limit boundary: "at or above" 500
    r.send('orders/create', order(1017, 'Noor', 'Haddad', lines=(('rug', 1),)))
    at_limit = r.alerts()
    r.send('orders/create', order(1018, 'Eli', 'Brandt', lines=(('rug_sale', 1),)))
    below = r.alerts()
    r.check('VIP limit boundary', 'a total of exactly 500.00 is VIP, 499.99 is not',
            val(items_for('#1017')[0], 'total') == '500.00' and val(items_for('#1017')[0], 'flags') == ['VIP'] and len(at_limit) == 1
            and at_limit[0].startswith('*VIP order #1017*') and val(items_for('#1018')[0], 'total') == '499.99'
            and val(items_for('#1018')[0], 'flags') is None and not below, f"#1017 {val(items_for('#1017')[0], 'flags')}, #1018 {val(items_for('#1018')[0], 'flags')}")

    # Rush keywords match in any case
    r.send('orders/create', order(1019, 'Kai', 'Moreno', lines=(('mug', 1),), shipping='USPS PRIORITY MAIL'))
    alerts = r.alerts()
    r.check('Shipping "USPS PRIORITY MAIL"', 'rush keyword "priority" matches in any case',
            val(items_for('#1019')[0], 'flags') == ['Rush'] and len(alerts) == 1 and alerts[0].startswith('*Rush order #1019*'),
            alerts[0].splitlines()[0] if alerts else 'no alert')

    # Order edits. Shopify keeps a replaced shipping line with is_removed: true, and a removed product stays in
    # line_items with current_quantity 0 (quantity keeps the original number).
    r.send('orders/create', order(1021, 'Rosa', 'Field', lines=(('tote', 1), ('mug', 2)), shipping='Express Overnight'))
    edited = order(1021, 'Rosa', 'Field', lines=(('tote', 1), ('mug', 2), ('candle', 1)), shipping='Express Overnight',
                   updated='2026-10-20T10:30:00-04:00')
    edited['shipping_lines'][0]['is_removed'] = True
    edited['shipping_lines'].append({'id': 5300000009999, 'title': 'Standard Shipping', 'code': 'Standard Shipping', 'price': '5.00',
                                     'source': 'shopify', 'carrier_identifier': None, 'discounted_price': '5.00', 'is_removed': False})
    edited['line_items'][1]['current_quantity'] = edited['line_items'][1]['fulfillable_quantity'] = 0
    r.send('orders/updated', edited)
    item = items_for('#1021')[0]
    r.check('Order edit: Express replaced by Standard, a line removed, a line added', 'Rush flag and Express gone, only the new line added, no alert',
            val(item, 'flags') is None and val(item, 'shipping_method') == 'Standard Shipping' and not r.alerts()
            and len(r.monday('create_subitem')) == 1 and subitems(item)[-1] == ('Soy Candle - Cedar', 'NWG-CNDL-CDR', '1') and len(item['subitems']) == 3,
            f"flags {val(item, 'flags')}, shipping {val(item, 'shipping_method')!r}, {len(item['subitems'])} subitems")
    late = order(1022, 'Omar', 'Quist', lines=(('tote', 3), ('mug', 1)), updated='2026-10-20T11:00:00-04:00')
    late['line_items'][0]['current_quantity'] = 1
    late['line_items'][1]['current_quantity'] = 0
    r.send('orders/updated', late)
    r.check('Edited order reaches the board for the first time', 'subitems use the current quantity and skip removed lines',
            items_for('#1022') and subitems(items_for('#1022')[0]) == [('Canvas Tote Bag - Natural', 'NWG-TOTE-NAT', '1')],
            json.dumps(subitems(items_for('#1022')[0])) if items_for('#1022') else 'no item')

    # Customer text is typed by the shopper. Slack markup in it must arrive as plain text, not as a mention or a link.
    r.send('orders/create', order(1020, '<!channel> Pat & <https://phish.example.com|Refund here>', 'Lee', lines=(('throw', 3),),
                                  email='pat.lee@example.com'))
    alerts = r.alerts()
    customer_line = next((line for line in (alerts[0].splitlines() if alerts else []) if line.startswith('Customer: ')), '')
    r.check('Slack markup in the customer name', 'escaped: no channel ping and no hidden link in the alert',
            len(alerts) == 1 and customer_line == 'Customer: &lt;!channel&gt; Pat &amp; &lt;https://phish.example.com|Refund here&gt; Lee (pat.lee@example.com)'
            and '<!channel>' not in alerts[0] and '<https://phish' not in alerts[0]
            and val(items_for('#1020')[0], 'customer') == '<!channel> Pat & <https://phish.example.com|Refund here> Lee',
            customer_line or 'no alert')

    # 7. An update for an order that is not on the board yet (placed before the workflow was switched on)
    r.send('orders/updated', order(1006, 'Alex', 'Kim', lines=(('tote', 1), ('candle', 2)), created='2026-10-19T17:00:00-04:00',
                                   updated='2026-10-20T08:00:00-04:00'))
    found = items_for('#1006')
    r.check('Update for an order not on the board', 'created with all its line items',
            len(found) == 1 and len(found[0]['subitems']) == 2 and len(r.monday('create_item')) == 1 and val(found[0], 'order_date') == {'date': '2026-10-19', 'time': '21:00:00'},
            f"{len(found)} item, {len(found[0]['subitems']) if found else 0} subitems")

    # Two items already carry the same order number (for example one added by hand)
    with LOCK:
        for _ in range(2):
            manual = new_item(BOARD_ID, '#1014 Robin Ellis', 'topics')
            manual['values'][COLUMNS['order_number']] = '#1014'
            ITEMS[manual['id']] = manual
    older, newer = items_for('#1014')
    r.send('orders/updated', order(1014, 'Robin', 'Ellis', lines=(('mug', 1),)))
    alerts = r.alerts()
    r.check('Two items already have order #1014', 'the oldest one is updated and Slack asks for a cleanup',
            val(older, 'payment_status') == 'Paid' and val(newer, 'payment_status') is None and len(items_for('#1014')) == 2
            and len(alerts) == 1 and f"Please merge or delete item {newer['id']}" in alerts[0], alerts[0].splitlines()[0] if alerts else 'no alert')
    SAMPLES['duplicate_alert'] = alerts[0] if alerts else ''

    # 8. orders/create and orders/updated race each other (both searches miss, both try to create)
    FAULT['create_delay'] = 1.2
    r.mark = len(LOG)
    lines = (('throw', 3), ('mug', 2))
    calls = [('orders/create', order(1007, 'Priya', 'Shah', lines=lines, financial='pending')),
             ('orders/updated', order(1007, 'Priya', 'Shah', lines=lines, financial='paid', updated='2026-10-20T09:15:09-04:00'))]
    timings = []
    threads = [threading.Thread(target=lambda c=c: timings.append(r.post(*c))) for c in calls]
    [t.start() for t in threads]
    [t.join() for t in threads]
    r.wait()
    FAULT['create_delay'] = 0.0
    found, creates = items_for('#1007'), r.monday('create_item')
    r.check('Create and update race (#1007)', 'one item and one set of subitems, never two',
            len(found) == 1 and [e['executed'] for e in creates].count(True) == 1 and any(e['conflict'] or e['replayed'] for e in creates)
            and len(found[0]['subitems']) == 2, f"{len(found)} item, {len(found[0]['subitems']) if found else 0} subitems, create attempts {len(creates)}")
    r.check('Create and update race (#1007)', 'the newer payload wins (payment Paid) and the VIP alert is sent once',
            found and val(found[0], 'payment_status') == 'Paid' and len(r.alerts()) == 1, f"payment {val(found[0], 'payment_status') if found else '?'}, {len(r.alerts())} alert")
    r.check('Create and update race (#1007)', 'Shopify got its 200 before monday finished',
            all(s == 200 and ms < 1200 for s, _, ms in timings), ', '.join(f'{ms} ms' for _, _, ms in timings) + ' (create_item took 1.2 s)')

    # 9. Rate limited by monday
    FAULT['rate_limit'] = 2
    status, _, _, ex = r.send('orders/create', order(1008, 'Chris', 'Doyle'))
    finds = r.monday('items_page_by_column_values')
    gaps = [round(b['t'] - a['t'], 1) for a, b in zip(finds, finds[1:])]
    r.check('monday rate limit (429 COMPLEXITY_BUDGET_EXHAUSTED twice)', 'waits retry_in_seconds, retries, then syncs once',
            ex['status'] == 'success' and [e['status'] for e in finds] == [429, 429, 200] and all(g >= 0.9 for g in gaps) and len(items_for('#1008')) == 1,
            f'search statuses {[e["status"] for e in finds]}, gaps {gaps} s')

    # monday answers 423 "Resource is currently locked" while another write holds the board
    FAULT['locked'] = 1
    status, _, _, ex = r.send('orders/updated', order(1008, 'Chris', 'Doyle', fulfillment='partial', updated='2026-10-20T14:00:00-04:00'))
    updates = r.monday('change_multiple_column_values')
    gaps = [round(b['t'] - a['t'], 1) for a, b in zip(updates, updates[1:])]
    r.check('Board locked (HTTP 423) during an update', 'waits Retry-After, retries once, then the update lands',
            ex['status'] == 'success' and [e['status'] for e in updates] == [423, 200] and all(g >= 0.9 for g in gaps)
            and val(items_for('#1008')[0], 'fulfillment_status') == 'Partially fulfilled' and len(items_for('#1008')) == 1,
            f'update statuses {[e["status"] for e in updates]}, gap {gaps} s')

    # 10. The create_item answer is lost (504) after monday created the item
    FAULT['lost_create'] = True
    status, _, _, ex = r.send('orders/create', order(1009, 'Dana', 'Reyes', lines=(('throw', 3),)))
    creates = r.monday('create_item')
    r.check('Answer to create_item lost (504)', 'retried with the same Idempotency-Key: one item, VIP alert still sent',
            ex['status'] == 'success' and [e['status'] for e in creates] == [504, 200] and creates[1]['replayed'] and creates[0]['key'] == creates[1]['key']
            and len(items_for('#1009')) == 1 and len(items_for('#1009')[0]['subitems']) == 1 and len(r.alerts()) == 1,
            f"create statuses {[e['status'] for e in creates]}, {len(items_for('#1009'))} item")

    # 11. GraphQL error inside an HTTP 200 answer
    FAULT['create_error'] = True
    status, _, _, ex = r.send('orders/create', order(1010, 'Riley', 'Park'))
    node, error = r.stopped_at(ex)
    alert = r.error_alert(ex)
    r.check('GraphQL error with HTTP 200 (ColumnValueException)', 'treated as a failure, not retried, nothing half written',
            ex['status'] == 'error' and node == 'Monday: Create Item' and 'ColumnValueException' in error and '#1010' in error
            and len(r.monday('create_item')) == 1 and not items_for('#1010') and not r.monday('create_subitem'), f'ended at {node}: {error[:90]}')
    SAMPLES['error_alert'] = alert
    r.check('GraphQL error with HTTP 200 (ColumnValueException)', 'error workflow posts the alert to Slack',
            'failed at *Monday: Create Item*' in alert and 'ColumnValueException' in alert, alert.splitlines()[0] if alert else 'no alert')

    # 12. A line item fails halfway, and the next update adds what is missing
    FAULT['subitem_error_in'] = 2
    status, _, _, ex = r.send('orders/create', order(1011, 'Jordan', 'Blake', lines=(('tote', 1), ('mug', 2), ('candle', 3))))
    node, error = r.stopped_at(ex)
    item = items_for('#1011')[0]
    r.check('Line item 2 of 3 fails', 'run flagged at Add Line Items, line 1 already on the board',
            node == 'Monday: Add Line Items' and 'Line 2 of 3' in error and len(item['subitems']) == 1, f'ended at {node}, {len(item["subitems"])} subitem')
    r.send('orders/updated', order(1011, 'Jordan', 'Blake', lines=(('tote', 1), ('mug', 2), ('candle', 3)), updated='2026-10-20T09:20:00-04:00'))
    r.check('Next update of #1011', 'adds the two missing lines and does not repeat the first',
            [s[0] for s in subitems(item)] == ['Canvas Tote Bag - Natural', 'Stoneware Mug - Slate', 'Soy Candle - Cedar'] and len(r.monday('create_subitem')) == 2,
            json.dumps([s[0] for s in subitems(item)]))

    # 13. monday is down / rejects the token
    FAULT['down'] = True
    status, _, _, ex = r.send('orders/create', order(1012, 'Casey', 'Nguyen'))
    FAULT['down'] = False
    node, error = r.stopped_at(ex)
    alert = r.error_alert(ex)
    r.check('monday down (HTTP 500)', 'retried 3 times, run flagged, alert sent',
            status == 200 and len(r.monday()) == 3 and node == 'Monday: Find Order' and 'HTTP 500' in error and 'failed at *Monday: Find Order*' in alert,
            f'{len(r.monday())} attempts, ended at {node}' + ('' if 'HTTP 500' in error and alert else f' | error={error!r} alert={alert!r}'))
    FAULT['unauthorized'] = True
    status, _, _, ex = r.send('orders/create', order(1012, 'Casey', 'Nguyen'))
    FAULT['unauthorized'] = False
    node, error = r.stopped_at(ex)
    r.check('monday rejects the token (HTTP 401)', 'not retried, run flagged',
            len(r.monday()) == 1 and node == 'Monday: Find Order' and 'HTTP 401' in error, f'{len(r.monday())} attempt, ended at {node}')

    # 14. Odd orders
    guest = order(1013, 'José', 'Martínez', lines=(('lamp', 1),), shipping=None, guest=True)
    status, _, _, ex = r.send('orders/create', guest, pretty=True)
    found = items_for('#1013')
    r.check('Guest checkout, no shipping, accented name, pretty printed JSON', 'signature still valid, customer from billing name, "No shipping"',
            status == 200 and len(found) == 1 and found[0]['name'] == '#1013 José Martínez' and val(found[0], 'shipping_method') == 'No shipping'
            and val(found[0], 'customer') == 'José Martínez', found[0]['name'] if found else f'{status}, no item')
    r.send('orders/updated', order(1003, 'Sam', 'Cole', lines=(('candle', 1),), shipping='Express Overnight', financial='refunded',
                                   cancelled='2026-10-20T12:00:00-04:00', updated='2026-10-20T12:00:05-04:00'))
    item = items_for('#1003')[0]
    r.check('Order #1003 cancelled and refunded', 'board shows Cancelled and Refunded, no alert',
            val(item, 'fulfillment_status') == 'Cancelled' and val(item, 'payment_status') == 'Refunded' and val(item, 'total') == '0.00' and not r.alerts(),
            f"{val(item, 'fulfillment_status')}, {val(item, 'payment_status')}, total {val(item, 'total')}")
    status, _, _, ex = r.send('products/update', {'id': 8800000000001, 'title': 'Canvas Tote Bag', 'admin_graphql_api_id': 'gid://shopify/Product/8800000000001'})
    node, _ = r.stopped_at(ex)
    r.check('Other topic (products/update)', 'answered 200 and ignored', status == 200 and not r.monday() and node == 'Skip: Repeat or Other Topic', f'ended at {node}')

    # 15. Slack is down when a VIP order comes in
    FAULT['slack_down'] = True
    status, _, _, ex = r.send('orders/create', order(1015, 'Avery', 'Stone', lines=(('throw', 3),)))
    FAULT['slack_down'] = False
    node, _ = r.stopped_at(ex)
    tries = [e for e in LOG[r.mark:] if e['service'] == 'slack' and (e['text'] or '').startswith('*VIP order #1015*')]
    r.check('Slack down when a VIP order arrives', 'the item is on the board, Slack retried 3 times, run flagged',
            len(items_for('#1015')) == 1 and len(items_for('#1015')[0]['subitems']) == 1 and len(tries) == 3 and node == 'Slack: Order Alert',
            f'{len(tries)} Slack attempts, ended at {node}')

    # 16. A typo in the column map (a copy of the workflow with "ordernumber" instead of "order_number")
    c.mark = len(LOG)
    c.post('orders/create', order(1016, 'Quinn', 'Hayes'))
    ex = c.wait()[-1]
    node, error = c.stopped_at(ex)
    c.check('Typo in MONDAY_COLUMNS', 'clear config error, nothing sent to monday',
            node == 'Prepare Order' and error.startswith('Unknown key ordernumber in MONDAY_COLUMNS') and not c.monday(), f'{error[:70]}')
    r.results += c.results

    # 17. Strict mock: nothing the workflow sent was rejected by the mock's own checks
    rejected = [(e['fields'], e['rejected']) for e in LOG if e['service'] == 'monday' and e['rejected']]
    total = len([e for e in LOG if e['service'] == 'monday'])
    r.check('Every monday request', 'valid against the schema, column formats, labels and headers', not rejected,
            f'{total} requests, {len(rejected)} rejected' + (f': {rejected[:2]}' if rejected else ''))


def main():
    if not KEY:
        raise SystemExit('Set N8N_API_KEY first.')
    server = ThreadingHTTPServer(('127.0.0.1', MOCK_PORT), Mock)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    created = []
    try:
        # Every temporary copy carries the same name, so leftovers are easy to find and delete.
        err_id = import_workflow(TEMP_NAME, test_copy(None, ENV))
        created.append(err_id)
        wid = import_workflow(TEMP_NAME, test_copy(PATH, ENV), {'errorWorkflow': err_id})
        created.append(wid)
        # No secret: the variable is not set at all (undefined in n8n), the way a forgotten variable looks
        vid = import_workflow(TEMP_NAME, test_copy(PATH + '-nosecret', {**ENV, 'SHOPIFY_WEBHOOK_SECRET': None}, str(uuid.uuid4())),
                              {'errorWorkflow': err_id})
        created.append(vid)
        bid = import_workflow(TEMP_NAME, test_copy(PATH + '-blanksecret', {**ENV, 'SHOPIFY_WEBHOOK_SECRET': '   '}, str(uuid.uuid4())),
                              {'errorWorkflow': err_id})
        created.append(bid)
        bad_columns = json.dumps({'ordernumber' if k == 'order_number' else k: v for k, v in COLUMNS.items()})
        cid = import_workflow(TEMP_NAME, test_copy(PATH + '-badconfig', {**ENV, 'MONDAY_COLUMNS': bad_columns}, str(uuid.uuid4())))
        created.append(cid)
        r, v, c = Runner(wid, PATH), Runner(vid, PATH + '-nosecret'), Runner(cid, PATH + '-badconfig')
        b = Runner(bid, PATH + '-blanksecret')
        for w in (err_id, wid, vid, bid, cid):     # n8n 2.x only runs an error workflow that is active
            api('POST', f'/api/v1/workflows/{w}/activate')
        time.sleep(2)
        try:
            run(r, v, c, b)
        except Exception as exc:      # a broken workflow can leave a scenario without the data a check expects
            traceback.print_exc()
            r.results.append(('Test run', 'all scenarios ran to the end', False, f'{type(exc).__name__}: {exc}'))
    finally:
        for w in reversed(created):
            try:
                api('POST', f'/api/v1/workflows/{w}/deactivate')
            except Exception:
                pass
            api('DELETE', f'/api/v1/workflows/{w}')
        server.shutdown()

    passed = sum(ok for *_, ok, _ in r.results)
    last = None
    for scenario, label, ok, detail in r.results:
        if scenario != last:
            print(f'\n{scenario}')
            last = scenario
        print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f'  ({detail})' if detail else ''))
    scenarios = len({scenario for scenario, *_ in r.results})
    monday_requests = len([e for e in LOG if e['service'] == 'monday'])
    print(f'\n{passed}/{len(r.results)} checks passed across {scenarios} scenarios ({monday_requests} monday requests)')
    if os.environ.get('SHOW_REQUESTS'):
        print('\nSample requests')
        for name, sample in SAMPLES.items():
            print(f'\n[{name}]\n' + (sample if isinstance(sample, str) else json.dumps(sample, indent=2, ensure_ascii=False)))
    sys.exit(0 if passed == len(r.results) else 1)


if __name__ == '__main__':
    main()
