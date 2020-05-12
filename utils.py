import os

import redis
import redis_lock


redis_client = redis.Redis.from_url(os.environ['REDIS_URL'])


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
