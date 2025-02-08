"""
LRU caching for Matrix federation request results.

Provides caching for federation API responses to reduce load on remote Matrix servers
and improve response times for repeated requests. Used by FederationApi to cache:
- DNS lookup results
- Server key responses
- Well-known delegation results
- Version API responses

The cache automatically removes stale entries through a background cleanup task.

Type Parameters:
    Throughout this module, generic type variables are used:
    - KT: Type of cache keys (usually str or EventID)
    - VT: Type of cached values (server responses, DNS results etc)
"""

from __future__ import annotations

from typing import Callable, Generic, Literal, TypeVar, overload
from dataclasses import dataclass
from threading import Lock
import asyncio
import time

KT = TypeVar("KT")
VT = TypeVar("VT")
CACHE_EXPIRY_TIME_SECONDS = 30 * 60  # 30 minutes
CACHE_CLEANUP_SLEEP_TIME_SECONDS = 30.0  # 30 seconds


@dataclass(slots=True)
class CacheEntry(Generic[VT]):
    """
    Base cache entry container for storing values.

    Attributes:
        cache_value: The stored value
    """

    cache_value: VT


@dataclass(slots=True)
class LRUCacheEntry(CacheEntry[VT]):
    """
    LRU cache entry that tracks access time.

    Attributes:
        last_access_time_ms: Timestamp of last access
        cache_value: The stored value (inherited)
    """

    last_access_time_ms: float


class LRUCache(Generic[KT, VT]):
    """
    Thread-safe LRU cache with background cleanup.

    Provides a generic cache implementation with configurable expiry and cleanup.
    Thread-safe access is ensured via Lock.

    Args:
        expire_after_seconds:
        cleanup_task_sleep_time_seconds:
        eviction_condition_func:

    Attributes:
        time_cb: Function that returns current time as float
        eviction_condition_cb: Function that determines if entry should be evicted
        expire_time_seconds: Time in seconds before entries expire (default 30 minutes)
        cleanup_iteration_sleep_seconds: Seconds between cleanup runs (default 30.0 seconds)
        _cleanup_task: Background task that removes expired entries
    """

    def __init__(
        self,
        expire_after_seconds: float = CACHE_EXPIRY_TIME_SECONDS,
        cleanup_task_sleep_time_seconds: float = CACHE_CLEANUP_SLEEP_TIME_SECONDS,
        eviction_condition_func: Callable[[LRUCacheEntry], bool] | None = None,
    ) -> None:
        """
        Initialize new LRU cache.

        Args:
            expire_after_seconds: Time in seconds before entries expire
            cleanup_task_sleep_time_seconds: Seconds between cleanup runs
            eviction_condition_func: Optional custom eviction condition
        """
        self._cache: dict[KT, LRUCacheEntry[VT]] = {}
        self._lock = Lock()
        self.time_cb = time.time
        self.eviction_condition_cb = eviction_condition_func or self.default_expiry_condition
        self.expire_time_seconds = expire_after_seconds
        self.cleanup_iteration_sleep_seconds = cleanup_task_sleep_time_seconds
        self._cleanup_task = asyncio.create_task(self._cleanup_cache_task())
        self.get = self.__getitem__
        self.set = self.__setitem__

    def __setitem__(self, key: KT, value: VT) -> None:
        """
        Set cache entry and update access time.

        Args:
            key: Cache key
            value: Value to store
        """
        with self._lock:
            self._cache[key] = LRUCacheEntry(cache_value=value, last_access_time_ms=self.time_cb())

    def __getitem__(self, item: KT, _default: VT | None = None) -> VT | None:
        """
        Get cache entry and update access time.

        Args:
            item: Cache key to retrieve
            _default: Value to return if key not found

        Returns:
            Cached value or default if not found
        """
        with self._lock:
            if cache_entry := self._cache.get(item):
                cache_entry.last_access_time_ms = self.time_cb()
                return cache_entry.cache_value
        return _default

    def __len__(self) -> int:
        """
        Get number of cache entries.

        Returns:
            Current number of entries in cache
        """
        return len(self._cache)

    def default_expiry_condition(self, cache_entry: LRUCacheEntry) -> bool:
        """
        Check if cache entry has exceeded its expiry time.

        Args:
            cache_entry: Entry to check for expiry

        Returns:
            True if entry has exceeded expiry time, False if still valid
        """
        return self.time_cb() > cache_entry.last_access_time_ms + self.expire_time_seconds

    async def _cleanup_cache_task(self) -> None:
        """
        Remove expired entries from cache periodically.

        Runs in background, sleeping between iterations to avoid early CPU spikes.
        Uses a dict copy during iteration since the cache view may change during cleanup.
        """
        while True:
            await asyncio.sleep(self.cleanup_iteration_sleep_seconds)
            for cache_key, cache_entry in dict(self._cache).items():
                if self.eviction_condition_cb(cache_entry):
                    self._cache.pop(cache_key, None)

    async def stop(self) -> None:
        """
        Stop the background cleanup task.

        Should be called when cache is no longer needed to clean up resources.
        """
        self._cleanup_task.cancel()
        await asyncio.gather(self._cleanup_task, return_exceptions=True)


@dataclass(slots=True)
class TTLCacheEntry(CacheEntry[VT]):
    ttl: int


class TTLCache(Generic[KT, VT]):
    """
    A basic TTLCache, with on-the-fly changes to TTL during entry creation.

    Initialize with a TTL for entries, or a default of 1 hour will be used. While
    setting new entries, you can change that value to be more or less. This way you
    can set a custom TTL for a given entry

    Attributes:
        _cache:
        _ttl_default_ms:
        _time_cb: the time callback to get the current time, returns time in seconds as a float
        get:
        set:

    """

    _cache: dict[KT, TTLCacheEntry[VT]]
    _ttl_default_ms: int
    _time_cb: Callable[..., float]

    def __init__(self, ttl_default_ms: int = 1 * 60 * 60 * 1000):
        self._time_cb = time.time
        self._cache = {}
        self._ttl_default_ms = ttl_default_ms
        self.get = self.__getitem__
        self.set = self.__setitem__

    def __setitem__(self, key: KT, value: VT, ttl_displacer_ms: int | None = None) -> None:
        ttl = int(self._time_cb() * 1000)
        if ttl_displacer_ms:
            ttl = ttl + ttl_displacer_ms
        else:
            ttl = ttl + self._ttl_default_ms
        self._cache[key] = TTLCacheEntry(cache_value=value, ttl=ttl)

    @overload
    def __getitem__(self, key: KT, _return_raw: Literal[False] = False) -> VT | None: ...  # noqa: E704

    @overload
    def __getitem__(self, key: KT, _return_raw: Literal[True] = True) -> TTLCacheEntry[VT] | None: ...  # noqa: E704

    def __getitem__(self, key: KT, _return_raw: bool = False) -> TTLCacheEntry[VT] | VT | None:
        if cache_entry := self._cache.get(key, None):
            if cache_entry.ttl < self._time_cb():
                self._cache.pop(key)
            if _return_raw:
                return cache_entry
            return cache_entry.cache_value
        return None

    def __len__(self) -> int:
        return len(self._cache)
