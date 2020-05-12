import os
from urllib.parse import urlencode

import redis
import redis_lock
from fulfil_client.client import dumps
from jinja2 import Environment, FileSystemLoader, select_autoescape


jinja_env = Environment(
    loader=FileSystemLoader('templates'),
    autoescape=select_autoescape(['html', 'xml'])
)
redis_client = redis.Redis.from_url(os.environ['REDIS_URL'])


def client_url(merchant_id, model, id=None, domain=None, **params):
    """
    A filter for template engine to generate URLs that open
    on fulfil client of the instance::
        <a href="{{ 'product.product'|client_url }}">Product List</a>
        <a href="{{ 'product.product'|client_url(product.id) }}">
                 {{ product.name }}</a>
    A more sophisticated example with filter
        <a href=
        "{{ 'product.product'|client_url(domain=[('salable', '=', True)]) }}">
            Salable Products
        </a>
    """
    url = f'https://{merchant_id}.fulfil.io/client/#/model/{model}'
    if id:
        url += '/%d' % id
    elif domain:
        params['domain'] = dumps(domain)
    if params:
        url += '?' + urlencode(params)
    return url


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
