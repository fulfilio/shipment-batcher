# Shipment batching/waving

This example report provides a command line interface to use over
the Fulfil API to create shipping batches.


## Setup

### Install dependencies

```
pip install -r requirements.txt
```

Will install all the necessary dependencies

### Configuration

Most of the configuration can be done using environment variables

* `FULFIL_WAREHOUSE_ID`: ID of the warehouse where waving should happen
* `FULFIL_MERCHANT_ID`: The subdomain of your fulfil instance.
* `FULFIL_ACCESS_TOKEN`: Oauth offline access token
* `SLACK_WEBHOOK`: A slack webhook url if you need slack notifications
* `REDIS_URL`: URL of the redis instance. needed for locks.


## Usage

### Help

Run `python batcher.py --help` to see all options.

```

Options:
  -d, --dry              Dry run instead of real batches
  -p, --priority         Separate priority batches
  -s, --single           Separate single item batches
  -m, --multi            Separate multi batches
  -a, --assorted_single  Separate assorted single line item batches
  -l, --leftovers        Add all left overs to another batch
  --poc                  POC mode.
  --start TEXT           Start date to pick shipments.
  --end TEXT             End date to pick shipments.
  --help                 Show this message and exit.
```

### Dry runs

If you don't want to create batches, but only want to preview how the
batching changes are working, you can do so by passing the `--dry`
option to the cli.

```
python batcher.py --dry
```


### POC mode

If you want to test how the batching would have worked, use the POC mode.
This will automatically change to dry run and create a local folder with
consolidate pick lists of the shipment batches.

To test POCs with past data, you can optionally specify start and end date
to test for specific date ranges.

Example

```
python batcher.py --dry --poc --start=2020-05-12 --end=2020-05-12
```
