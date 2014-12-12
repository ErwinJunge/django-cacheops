# -*- coding: utf-8 -*-
import json
from collections import defaultdict, Counter
from funcy import memoize, post_processing, ContextDecorator
from django.db.models.expressions import ExpressionNode

from .conf import redis_client, handle_connection_failure, model_profile
from .utils import non_proxy, load_script, get_thread_id, NOT_SERIALIZED_FIELDS, get_related_objects


__all__ = ('invalidate_obj', 'invalidate_model', 'invalidate_all', 'no_invalidation')


_no_invalidation_depth = defaultdict(int)


@handle_connection_failure
def invalidate_dict(model, obj_dict):
    if _no_invalidation_depth.get(get_thread_id()):
        return
    model = non_proxy(model)
    load_script('invalidate')(args=[
        model._meta.db_table,
        json.dumps(obj_dict, default=str)
    ])

def invalidate_obj(obj, classes_handled=None):
    """
    Invalidates caches that can possibly be influenced by object
    """
    # FIXME: Reduce db queries by getting obj again with deep prefetch_related_arg argument
    profile = model_profile(obj.__class__)
    max_depth = profile['invalidate_related_objects_depth']
    if max_depth:
        if classes_handled is None:
            classes_handled = Counter()
        if classes_handled.get(obj.__class__, 0) <= max_depth:
            classes_handled[obj.__class__] += 1
            for item in get_related_objects(obj, classes_handled, max_depth):
                invalidate_obj(item, classes_handled)
    model = non_proxy(obj.__class__)
    invalidate_dict(model, get_obj_dict(model, obj))

@handle_connection_failure
def invalidate_model(model):
    """
    Invalidates all caches for given model.
    NOTE: This is a heavy artilery which uses redis KEYS request,
          which could be relatively slow on large datasets.
    """
    if _no_invalidation_depth.get(get_thread_id()):
        return
    model = non_proxy(model)
    conjs_keys = redis_client.keys('conj:%s:*' % model._meta.db_table)
    if conjs_keys:
        cache_keys = redis_client.sunion(conjs_keys)
        redis_client.delete(*(list(cache_keys) + conjs_keys))

@handle_connection_failure
def invalidate_all():
    if _no_invalidation_depth.get(get_thread_id()):
        return
    redis_client.flushdb()


class _no_invalidation(ContextDecorator):
    def __enter__(self):
        _no_invalidation_depth[get_thread_id()] += 1

    def __exit__(self, type, value, traceback):
        _no_invalidation_depth[get_thread_id()] -= 1

no_invalidation = _no_invalidation()


### ORM instance serialization

@memoize
def serializable_fields(model):
    return tuple(f for f in model._meta.fields
                   if not isinstance(f, NOT_SERIALIZED_FIELDS))

@post_processing(dict)
def get_obj_dict(model, obj):
    for field in serializable_fields(model):
        value = getattr(obj, field.attname)
        if value is None:
            yield field.attname, None
        elif isinstance(value, ExpressionNode):
            continue
        else:
            yield field.attname, field.get_prep_value(value)
