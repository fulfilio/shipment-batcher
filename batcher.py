import os
from collections import defaultdict, Counter
from datetime import datetime
from itertools import groupby
import webbrowser

import click
import requests
from more_itertools import chunked
from dateutil.relativedelta import relativedelta
from dateutil.parser import parse as parse_date
from fulfil_client import Client, BearerAuth

from jinja2 import Environment, FileSystemLoader, select_autoescape

import redis
import redis_lock

jinja_env = Environment(
    loader=FileSystemLoader('templates'),
    autoescape=select_autoescape(['html', 'xml'])
)
redis_client = redis.Redis.from_url(os.environ['REDIS_URL'])

# Minimum number of shipments that should exist in a batch
# before that gets created. If there is insufficient quantity
# there is not separate batch created.
BATCH_SIZE_MIN = int(os.environ.get('BATCH_SIZE_MIN', 5))

# Maximum size of the batch that should be created.
# This usually corresponds to the number of totes in
# a picking cart.
BATCH_SIZE_MAX = int(os.environ.get('BATCH_SIZE_MIN', 25))

# Single unit batch cap
BATCH_SIZE_SINGLE_UNITS_MIN = int(os.environ.get('BATCH_SIZE_MIN', 24))
BATCH_SIZE_SINGLE_UNITS_MAX = int(os.environ.get('BATCH_SIZE_MIN', 500))

# Single line item caps
BATCH_SIZE_SINGLE_MIN = int(os.environ.get('BATCH_SIZE_MIN', BATCH_SIZE_MIN))
BATCH_SIZE_SINGLE_MAX = int(os.environ.get('BATCH_SIZE_MIN', BATCH_SIZE_MAX))

# A set of categories that should create it's own
# batches. The only exclusion to this is high priority
# where priority takes presedence over the category
SPECIAL_CATEGORIES = set([
    # 'Category name 1',
    # 'Category name 2',
])

# Number of days forward to look at when finding shipments
# to ship. The shipments should still be ready to pick.
FUTURE_DAYS = 1

WAREHOUSE_ID = int(os.environ['FULFIL_WAREHOUSE_ID'])
FULFIL_MERCHANT_ID = os.environ['FULFIL_MERCHANT_ID']
FULFIL_ACCESS_TOKEN = os.environ['FULFIL_OFFLINE_ACCESS_TOKEN']

SLACK_WEBHOOK = os.environ.get('SLACK_WEBHOOK')

# A sorting function that sorts by the location
# of the first item in a shipment. This works best with
# single line shipments and still optimizes multi-line
# shipments.
LOCATION_SORT_KEY = lambda s: (      # noqa
    s['inventory_moves'][0]['from_location_sequence'],
    s['inventory_moves'][0]['from_location'],
    s['inventory_moves'][0]['from_sublocation_sequence'] or 10,
    s['inventory_moves'][0]['from_sublocation_name'] or 'Z-LOC',
    s['inventory_moves'][0]['product']['code']
)


fulfil = Client(
    FULFIL_MERCHANT_ID, FULFIL_ACCESS_TOKEN
)


class NonBlockingLock(redis_lock.Lock):
    """
    The default lock is blocking and gets the clock stuck :(
    """
    def __init__(self, name, redis_client=redis_client,
                 expire=15*60, id=None, auto_renewal=True,
                 strict=True):
        return super(NonBlockingLock, self).__init__(
            redis_client, name,
            expire, id, auto_renewal, strict
        )

    def __enter__(self):
        acquired = self.acquire(blocking=False)
        assert acquired, "Lock wasn't acquired"
        return self


def create_optimal_batches(shipments=None, **kwargs):
    """
    Create optimal shipping batches.

    :param shipments: A list of shipment object. If not
                      provided, reads all ready to pick,
                      unbatched shipments.
    """
    # Lock to avoid this process being run in parallel
    # by two processes that are connected to the same
    # redis.
    with NonBlockingLock('batching'):
        return _create_optimal_batches(shipments, **kwargs)


def _create_optimal_batches(
        shipments=None,
        dry=False,
        priority=None,
        single=True,
        assorted_single=True,
        multi=True,
        leftovers=False):
    """
    Create shipment batches based on the provided parameters

    :param shipments: optionally provide the list of shipments to batch
    :param dry: Just prints how the batches would be created, but does
                not actually created batches. A dry run.
    :param priority: Handles priority shipments.
                     * None: No separate prioritization needed
                     * True: Create a separate priority batch.
                     * False: Don't create priority batches in this run.
    :param single: Single line item shipments of the same item.
    :param multi: Multi line item shipments of the same item.
    :param assorted_single: A collection os single line item shipments
                            across different items.
    :param leftovers: Include leftovers from other batch creation.
    """
    ShipmentBatch = fulfil.model('stock.shipment.out.batch')

    if not shipments:
        # If a list of shipments is not explicitly set
        # fetch all unbatched shipments.
        shipments = get_shipments(WAREHOUSE_ID)

    priority_shipments = []
    multi_shipments = []
    single_shipments = []
    single_unit_shipments = []
    special_category_shipments = defaultdict(list)

    for shipment in shipments:
        # Bucket each shipment into one of the above
        # categories.
        if priority is not None and shipment['priority'] in ('0', '1'):
            priority_shipments.append(shipment)

        elif shipment['product_categories'] & SPECIAL_CATEGORIES:
            category_intersection = list(
                shipment['product_categories'] & SPECIAL_CATEGORIES
            )
            if len(category_intersection) > 1:
                category_name = "Multi "
            else:
                category_name = category_intersection[0]
            special_category_shipments[category_name].append(shipment)

        elif shipment['total_quantity'] == 1:
            single_unit_shipments.append(shipment)

        elif len(shipment['inventory_moves']) == 1:
            single_shipments.append(shipment)

        else:
            multi_shipments.append(shipment)

    if priority:
        tag_batch(
            sorted(
                priority_shipments,
                key=LOCATION_SORT_KEY
            ),
            "*Priority batch*"
        )
        if leftovers:
            move_unbatched_from(priority_shipments, multi_shipments)

    if single:
        shipments_by_sku = _get_sku_counts(single_unit_shipments)
        for sku, count in shipments_by_sku.most_common():
            if count >= BATCH_SIZE_SINGLE_UNITS_MIN:
                _shipments = [
                    s for s in single_unit_shipments
                    if s['inventory_moves'][0]['product']['code'] == sku
                ]
                tag_batch(
                    _shipments,
                    "Single item batch: {}".format(sku),
                    cap=BATCH_SIZE_SINGLE_UNITS_MAX
                )

        # Now add all the unbatched single unit shipments into the
        # assorted single batch
        move_unbatched_from(single_unit_shipments, single_shipments)

    if assorted_single:
        single_shipments = sorted(single_shipments, key=LOCATION_SORT_KEY)
        tag_batch(
            single_shipments,
            "Assorted single batch",
            cap=BATCH_SIZE_SINGLE_MAX
        )

        if leftovers:
            # Add remaining single shipments into the multi batches
            move_unbatched_from(single_shipments, multi_shipments)

    if multi:
        # Rank shipments by most common SKUs
        sku_counter = _get_sku_counts(multi_shipments)
        most_common_skus = [el[0] for el in sku_counter.most_common()]
        rank_key = lambda shipment: min([       # noqa
            most_common_skus.index(move['product']['code'])
            for move in shipment['inventory_moves']
        ])
        multi_shipments = sorted(multi_shipments, key=rank_key)
        tag_batch(multi_shipments, "Multi-item batch")

    if single or multi:
        for category_name, cat_shipments in special_category_shipments.items():
            sku_counter = _get_sku_counts(cat_shipments)
            most_common_skus = [el[0] for el in sku_counter.most_common()]
            rank_key = lambda shipment: min([       # noqa
                most_common_skus.index(move['product']['code'])
                for move in shipment['inventory_moves']
            ])
            cat_shipments = sorted(cat_shipments, key=rank_key)
            tag_batch(cat_shipments, "Category: {}".format(category_name))

    batch_data = _get_batches_table(shipments)
    print(batch_data)

    if SLACK_WEBHOOK:
        post_to_slack(batch_data, dry)

    if dry:
        print("Dry Run. No real batches were created.")
    else:
        today = fulfil.today()
        key = lambda s: s.get('batch_name', '*Unbatched')   # noqa
        batch_ids = []
        for batch_name, b_shipments in groupby(
                sorted(shipments, key=key), key=key):
            if batch_name == '*Unbatched':
                continue
            b_shipments = list(b_shipments)
            print([s['id'] for s in b_shipments])
            batch_ids = ShipmentBatch.create([{
                'name': '{}/{}'.format(today.isoformat(), batch_name,),
                'warehouse': WAREHOUSE_ID,
                'shipments': [('add', [s['id'] for s in b_shipments])]
            }])
            # for shipment in b_shipments:
            #     print(shipment['id'])
            #     ShipmentBatch.write(
            #         batch_ids,
            #         {'shipments': [('add', [shipment['id']])]}
            #     )
            ShipmentBatch.open(batch_ids)
    return shipments


def post_to_slack(batch_data, dry):
    """
    Post the shipment data to slack
    """
    content = [
        '```',
        batch_data,
        '```',
    ]
    if dry:
        content.append('Dry Run. No batches created.')
    requests.post(
        SLACK_WEBHOOK,
        json={
            'text': '\n'.join(content)
        }
    )


def _get_batches_table(shipments):
    """
    Get an ASCII table
    """
    from terminaltables import AsciiTable

    shipment_counter = Counter()
    item_counter = Counter()
    key = lambda s: s.get('batch_name', '*Unbatched')   # noqa
    for batch_name, b_shipments in groupby(
            sorted(shipments, key=key), key=key):
        b_shipments = list(b_shipments)
        shipment_counter[batch_name] += len(b_shipments)
        for shipment in b_shipments:
            item_counter[batch_name] += len(shipment['inventory_moves'])

    data = [
        ['Batch Name', '# of Shipments', '# of items']
    ]
    for batch_name in shipment_counter:
        data.append([
            batch_name,
            shipment_counter[batch_name],
            item_counter[batch_name],
        ])
    data.append([
        "**Total**",
        sum(shipment_counter.values()),
        sum(item_counter.values()),
    ])
    return AsciiTable(data).table


def get_shipments(
        warehouse_id=WAREHOUSE_ID,
        start_date=None,
        end_date=None,
        unpicked=True,
        unbatched=True):
    """
    Return shipments that qualify to be batched

    :param unbatched: If set to false this will override
                      already batched shipments.
    """
    StockMove = fulfil.model('stock.move')

    domain = [
        ('shipment.carrier', '!=', None, 'stock.shipment.out'),
        ('shipment.delivery_mode', '=', 'ship', 'stock.shipment.out'),
        ('from_location.type', '=', 'storage'),
        ('to_location.type', '=', 'storage'),
        ('shipment.warehouse', '=', warehouse_id, 'stock.shipment.out'),
        ('shipment.on_hold', '=', False, 'stock.shipment.out'),
        ('state', '!=', 'cancel'),
        ('quantity', '>', 0),
    ]

    if start_date is not None:
        domain.append(
            ('shipment.planned_date', '>=', start_date, 'stock.shipment.out')
        )

    if end_date is not None:
        domain.append(
            ('shipment.planned_date', '<=', end_date, 'stock.shipment.out')
        )
    else:
        today = fulfil.today()
        end_date = today + relativedelta(days=FUTURE_DAYS)
        domain.append(
            ('shipment.planned_date', '<=', end_date, 'stock.shipment.out')
        )

    if unpicked:
        domain.extend([
            ('shipment.state', '=', 'assigned', 'stock.shipment.out'),
            ('shipment.picking_status', '=', None, 'stock.shipment.out'),
        ])

    if unbatched:
        domain.append(
            ('shipment.shipping_batch', '=', None, 'stock.shipment.out')
        )

    print(domain)
    fields = [
        'product',
        'product.code',
        'product.template.account_category.name',
        'from_location.sequence',
        'from_location',
        'from_sublocation.sequence',
        'from_sublocation.name',
        'shipment.total_quantity',
        'shipment.priority',
        'shipment.planned_date',
        'shipment',
        'shipment.channels',
        'shipment.number',
    ]

    count = StockMove.search_count(domain)
    print("Found {} stock movements".format(count))
    move_ids = StockMove.search(domain)
    moves = []
    for index, ids in enumerate(chunked(move_ids, 500), 1):
        print(f"getting chunk {index}")
        moves.extend(StockMove.read(ids, fields))

    shipments = defaultdict(lambda: {
        'inventory_moves': [],
        'product_categories': set()
    })
    for move in moves:
        shipments[move['shipment']]['id'] = int(move['shipment'].split(',', 1)[-1])     # noqa
        shipments[move['shipment']]['channel'] = move['shipment.channels'] and move['shipment.channels'][0]     # noqa
        shipments[move['shipment']]['priority'] = move['shipment.priority']
        shipments[move['shipment']]['number'] = move['shipment.number']
        shipments[move['shipment']]['planned_date'] = move['shipment.planned_date']     # noqa
        shipments[move['shipment']]['total_quantity'] = move['shipment.total_quantity']     # noqa
        shipments[move['shipment']]['inventory_moves'].append({
            'product': {
                'id': move['product'],
                'code': move['product.code'],
                'category': move['product.template.account_category.name'],
            },
            'from_sublocation_name': move['from_sublocation.name'],
            'from_sublocation_sequence': move['from_sublocation.sequence'],
            'from_location_sequence': move['from_location.sequence'],
            'from_location': move['from_location'],
        })
        shipments[move['shipment']]['product_categories'].add(
            move['product.template.account_category.name']
        )
    return list(shipments.values())


def move_unbatched_from(from_batch, to_batch):
    """
    Move unbatched shipments from one list to another
    """
    for shipment in from_batch:
        if not shipment.get('batch_name'):
            to_batch.append(shipment)


def tag_batch(shipments, prefix, cap=BATCH_SIZE_MAX):
    """
    Tag all the shipments with the given batch prefix
    """
    print("Tagging {} shipments with prefix {}".format(
        len(shipments), prefix
    ))
    for index, chunk in enumerate(chunked(shipments, cap), 1):
        if len(chunk) < BATCH_SIZE_MIN:
            # Not enough to create a batch?
            # don't create the batch
            break

        name = f"{prefix} {index:0>3}"

        for shipment in chunk:
            if shipment.get('batch_name'):
                raise "Batch {} already exists for shipment {}".format(
                    shipment['batch_name'],
                    shipment['id']
                )
            shipment['batch_name'] = name


def _get_sku_counts(shipments):
    """
    Return a counter with the items in the shipments.
    """
    sku_counter = Counter()
    for shipment in shipments:
        for move in shipment['inventory_moves']:
            sku_counter[move['product']['code']] += 1
    return sku_counter


def create_reports(shipments):
    """
    Create a report from the shimpents
    """
    template = jinja_env.get_template('report.html')
    folder = os.path.join(
        'reports',
        FULFIL_MERCHANT_ID,
    )
    filename = "{}.html".format(datetime.utcnow().isoformat('T'))
    os.makedirs(folder, exist_ok=True)
    metrics = {
        'shipments': len(shipments),
        'line_items': 0,
        'total_quantity': 0,
        'single_line': 0,
        'multi_line': 0,
        'single_unit': 0,
        'products': []
    }
    for shipment in shipments:
        metrics['line_items'] += len(shipment['inventory_moves'])
        metrics['total_quantity'] += shipment['total_quantity']
        metrics['products'].extend([
            move['product']['code'] for move in shipment['inventory_moves']
        ])
        if shipment['total_quantity'] == 1:
            metrics['single_unit'] += 1
        elif len(shipment['inventory_moves']) == 1:
            metrics['single_line'] += 1
        else:
            metrics['multi_line'] += 1
    metrics['products'] = len(set(metrics['products']))

    path_to_file = os.path.join(folder, filename)
    with open(path_to_file, 'w') as f:
        key = lambda s: s.get('batch_name', '*Unbatched')   # noqa
        f.write(template.render(
            grouped_shipments=[
                (batch_name, list(batch_shipments))
                for (batch_name, batch_shipments) in
                groupby(sorted(shipments, key=key), key=key)
            ],
            metrics=metrics,
            shipment_sorter=lambda _shipments: sorted(
                _shipments, key=LOCATION_SORT_KEY
            )
        ))
    return path_to_file


def create_consolidated_pick_lists(shipments):
    """
    Create a local folder and save consolidated shipments.
    """
    report = fulfil.report('report.consolidated_picking_list')
    key = lambda s: s.get('batch_name', '*Unbatched')   # noqa
    folder = os.path.join(
        'reports',
        FULFIL_MERCHANT_ID,
        datetime.utcnow().isoformat('T')
    )
    os.makedirs(folder, exist_ok=True)
    print("Saving consolidated pick liss to {}".format(folder))
    for batch_name, b_shipments in groupby(
            sorted(shipments, key=key), key=key):
        data = report.execute(
            [s['id'] for s in b_shipments],
            context={'return_link': True}
        )
        filename = '{} {}'.format(batch_name, data['filename'])
        response = requests.get(data['url'])
        print(filename)
        with open(os.path.join(folder, filename), 'wb') as f:
            f.write(response.content)


@click.command()
@click.option('-d', '--dry', default=False, is_flag=True,
              help='Dry run instead of real batches')
@click.option('-p', '--priority', default=True, is_flag=True,
              help='Separate priority batches')
@click.option('-s', '--single', default=True, is_flag=True,
              help='Separate single item batches')
@click.option('-m', '--multi', default=True, is_flag=True,
              help='Separate multi batches')
@click.option('-a', '--assorted_single', default=True, is_flag=True,
              help='Separate assorted single line item batches')
@click.option('-l', '--leftovers', default=False, is_flag=True,
              help='Add all left overs to another batch')
@click.option('--poc', default=False, is_flag=True,
              help='POC mode.')
@click.option('--pdf', default=False, is_flag=True,
              help='Print consolidated picklists for batches')
@click.option('--start', default=None,
              help='Start date to pick shipments.')
@click.option('--end', default=None,
              help='End date to pick shipments.')
def create_batches_cli(
        dry, priority, single, assorted_single, multi, leftovers,
        poc, pdf, start, end):
    """
    Shipment batching system.

    ## POC mode

    If you want to test how the batching would have worked, use the POC
    mode. This will automatically change to dry run and create a local
    folder with a HTML report showing the batches created and the shipments
    in them.

    To test POCs with past data, you can optionally specify start and
    end date to test for specific date ranges.

    If you want to preview consolidated pick lists, you can pass `--pdf`
    and consolidated pick lists will also be saved to the report folder.

    ## End of day batches

    If this is being run EOD, then also pass -l to enable leftovers.
    Any shipment that did not get included in other batches will be
    picked into the multi batches.
    """
    if poc:
        dry = True

    if start is not None:
        start = parse_date(start)

    if end is not None:
        end = parse_date(end)

    shipments = None
    if start or end:
        dry = True
        shipments = get_shipments(
            start_date=start,
            end_date=end,
            unpicked=False,
            unbatched=False,
        )

    shipments = create_optimal_batches(
        shipments=shipments,
        dry=dry, priority=priority,
        single=single, assorted_single=assorted_single,
        multi=multi, leftovers=leftovers
    )

    if poc:
        filename = create_reports(shipments)
        webbrowser.open(filename)

    if pdf:
        create_consolidated_pick_lists(shipments)


if __name__ == '__main__':
    create_batches_cli()
