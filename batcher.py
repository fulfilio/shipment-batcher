import os
from collections import defaultdict, Counter
from itertools import groupby

import click
import requests
from more_itertools import chunked
from dateutil.relativedelta import relativedelta
from fulfil_client import Client, BearerAuth

from .utils import NonBlockingLock

# Maximum size of the batch that should be created.
# This usually corresponds to the number of totes in
# a picking cart.
BATCH_SIZE_MAX = 20

# Minimum number of shipments that should exist in a batch
# before that gets created. If there is insufficient quantity
# there is not separate batch created.
BATCH_SIZE_MIN = 5

# Number of days forward to look at when finding shipments
# to ship. The shipments should still be ready to pick.
FUTURE_DAYS = 1

WAREHOUSE_ID = int(os.environ['FULFIL_WAREHOUSE_ID'])
FULFIL_MERCHANT_ID = os.environ['FULFIL_MERCHANT_ID']
FULFIL_ACCESS_TOKEN = os.environ['FULFIL_ACCESS_TOKEN']

SLACK_WEBHOOK = os.environ.get('SLACK_WEBHOOK')

fulfil = Client(
    FULFIL_MERCHANT_ID,
    auth=BearerAuth(FULFIL_ACCESS_TOKEN)
)


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
        _create_optimal_batches(shipments, **kwargs)


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
        shipments = get_unbatched_shipments(WAREHOUSE_ID)

    priority_shipments = []
    multi_shipments = []
    single_shipments = []
    single_unit_shipments = []

    for shipment in shipments:
        # Bucket each shipment into one of the above
        # categories.
        if priority is not None and shipment['priority'] in ('0', '1'):
            priority_shipments.append(shipment)

        elif shipment['total_quantity'] == 1:
            single_unit_shipments.append(shipment)

        elif len(shipment['inventory_moves']) == 1:
            single_shipments.append(shipment)

        else:
            multi_shipments.append(shipment)

    if priority:
        tag_batch(priority_shipments, "*Priority batch*")
        if leftovers:
            move_unbatched_from(priority_shipments, to=multi_shipments)

    if single:
        shipments_by_sku = _get_sku_counts(single_unit_shipments)
        for sku, count in shipments_by_sku.most_common():
            if count >= BATCH_SIZE_MAX:
                _shipments = [
                    s for s in single_unit_shipments
                    if s['inventory_moves'][0]['product']['code'] == sku
                ]
                tag_batch(_shipments, "Single batch: {}".format(sku))

        # Now add all the unbatched single unit shipments into the
        # assorted single batch
        move_unbatched_from(single_unit_shipments, to=single_shipments)

    if assorted_single:
        sort_key = lambda s: (      # noqa
            s['inventory_moves'][0]['from_location_sequence'],
            s['inventory_moves'][0]['from_location'],
            s['inventory_moves'][0]['warehouse_location_name'] or 'Z-LOC',
            s['inventory_moves'][0]['product']['code']
        )
        single_shipments = sorted(single_shipments, key=sort_key)
        tag_batch(single_shipments, "Assorted single batch")

        if leftovers:
            # Add remaining single shipments into the multi batches
            move_unbatched_from(single_shipments, to=multi_shipments)

    if multi:
        # Rank shipments by most common SKUs
        sku_counter = _get_sku_counts(shipments)
        most_common_skus = [el[0] for el in sku_counter.most_common()]
        rank_key = lambda shipment: min([       # noqa
            most_common_skus.index(move['product']['code'])
            for move in shipment['inventory_moves']
        ])
        multi_shipments = sorted(multi_shipments, key=rank_key)
        tag_batch(multi_shipments, "Multi-item batch")

    batch_data = _get_batches_table(shipments)
    print(batch_data)

    if SLACK_WEBHOOK:
        post_to_slack(batch_data, dry)

    if dry:
        print("Dry Run. No real batches were created.")
        return

    today = fulfil.today()
    key = lambda s: s.get('batch_name', '*Unbatched')   # noqa
    batch_ids = []
    for batch_name, b_shipments in groupby(
            sorted(shipments, key=key), key=key):
        if batch_name == '*Unbatched':
            continue
        batch_ids = ShipmentBatch.create([{
            'name': '{}/{}'.format(today.isoformat(), batch_name,),
            'warehouse': WAREHOUSE_ID,
            'shipments': [('add', [s['id'] for s in b_shipments])]
        }])
        ShipmentBatch.open(batch_ids)


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
            batch_name, shipment_counter[batch_name], item_counter[batch_name]
        ])
    return AsciiTable(data).table


def get_unbatched_shipments(warehouse_id=WAREHOUSE_ID):
    """
    Return shipments that qualify to be batched
    """
    StockMove = fulfil.model('stock.move')
    today = fulfil.today()
    cutoff = today + relativedelta(days=FUTURE_DAYS)
    domain = [
        ('shipment.state', '=', 'assigned', 'stock.shipment.out'),
        ('shipment.on_hold', '=', False, 'stock.shipment.out'),
        ('shipment.warehouse', '=', warehouse_id, 'stock.shipment.out'),
        ('shipment.shipping_batch', '=', None, 'stock.shipment.out'),
        ('shipment.carrier', '!=', None, 'stock.shipment.out'),
        ('shipment.delivery_mode', '=', 'ship', 'stock.shipment.out'),
        ('from_location.type', '=', 'storage'),
        ('to_location.type', '=', 'storage'),
        ('shipment.planned_date', '<=', cutoff, 'stock.shipment.out'),
    ]
    fields = [
        'product',
        'product.code',
        'warehouse_location',
        'from_location.sequence',
        'from_location',
        'shipment.total_quantity',
        'shipment.priority',
        'shipment.planned_date',
        'shipment',
        'shipment.channels',
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
    })
    for move in moves:
        shipments[move['shipment']]['id'] = int(move['shipment'].split(',', 1)[-1])     # noqa
        shipments[move['shipment']]['channel'] = move['shipment.channels'] and move['shipment.channels'][0]     # noqa
        shipments[move['shipment']]['priority'] = move['shipment.priority']
        shipments[move['shipment']]['planned_date'] = move['shipment.planned_date']     # noqa
        shipments[move['shipment']]['total_quantity'] = move['shipment.total_quantity']     # noqa
        shipments[move['shipment']]['inventory_moves'].append({
            'product': {
                'id': move['product'],
                'code': move['product.code'],
            },
            'warehouse_location_name': move['warehouse_location'],
            'from_location_sequence': move['from_location.sequence'],
            'from_location': move['from_location'],
        })
    return list(shipments.values())


def move_unbatched_from(from_batch, to_batch):
    """
    Move unbatched shipments from one list to another
    """
    for shipment in from_batch:
        if not shipment.get('batch_name'):
            to_batch.append(shipment)


def tag_batch(shipments, prefix):
    """
    Tag all the shipments with the given batch prefix
    """
    print("Tagging {} shipments with prefix {}".format(
        len(shipments), prefix
    ))
    for index, chunk in enumerate(chunked(shipments, BATCH_SIZE_MAX), 1):
        if len(chunk) < BATCH_SIZE_MIN:
            # Not enough to create a batch?
            # don't create the batch
            break

        name = "{prefix} {:0>3}".format(prefix, index)

        for shipment in shipments:
            if shipment.get('batch_name'):
                raise "Batch {} already exists for shipment {}".format(
                    shipment['batch_name'],
                    shipment['number']
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


@click.command()
@click.option('-d', '--dry', default=False,
              help='Dry run instead of real batches')
@click.option('-p', '--priority', default=True,
              help='Separate priority batches')
@click.option('-s', '--single', default=True,
              help='Separate single item batches')
@click.option('-m', '--multi', default=True,
              help='Separate multi batches')
@click.option('-a', '--assorted_single', default=True,
              help='Separate assorted single line item batches')
@click.option('-l', '--leftovers', default=True,
              help='Add all left overs to another batch')
def create_batches_cli(
        dry, priority, single, assorted_single, multi, leftovers):
    """
    Shipment batching system.

    ## End of day batches

    If this is being run EOD, then also pass -l to enable leftovers.
    Any shipment that did not get included in other batches will be
    picked into the multi batches.
    """
    create_optimal_batches(
        dry=dry, priority=priority,
        single=single, assorted_single=assorted_single,
        multi=multi, leftovers=leftovers
    )


if __name__ == '__main__':
    create_batches_cli()
