# Copyright 2012,2014 Christoph Reiter
#
# This library is free software; you can redistribute it and/or
# modify it under the terms of the GNU Lesser General Public
# License as published by the Free Software Foundation; either
# version 2.1 of the License, or (at your option) any later version.

import os
import types
import sys
import imp
import warnings
from functools import wraps

from pgi import _compat
from pgi import const
from pgi.util import PyGIDeprecationWarning


# namespace -> (attr, replacement)
_deprecated_attrs = {}


class OverridesProxyModule(types.ModuleType):
    """Wraps a introspection module and contains all overrides"""

    def __init__(self, introspection_module):
        super(OverridesProxyModule, self).__init__(
            introspection_module.__name__)
        self._introspection_module = introspection_module

    def __getattr__(self, name):
        return getattr(self._introspection_module, name)

    def __dir__(self):
        result = set(dir(self.__class__))
        result.update(self.__dict__.keys())
        result.update(dir(self._introspection_module))
        return sorted(result)

    def __repr__(self):
        return "<%s %r>" % (type(self).__name__, self._introspection_module)


class _DeprecatedAttribute(object):
    """A deprecation descriptor for OverridesProxyModule subclasses.

    Emits a PyGIDeprecationWarning on every access and tries to act as a
    normal instance attribute (can be replaced and deleted).
    """

    def __init__(self, namespace, attr, value, replacement):
        self._attr = attr
        self._value = value
        self._warning = PyGIDeprecationWarning(
            '%s.%s is deprecated; use %s instead' % (
                namespace, attr, replacement))

    def __get__(self, instance, owner):
        if instance is None:
            raise AttributeError(self._attr)
        warnings.warn(self._warning, stacklevel=2)
        return self._value

    def __set__(self, instance, value):
        attr = self._attr
        # delete the descriptor, then set the instance value
        delattr(type(instance), attr)
        setattr(instance, attr, value)

    def __delete__(self, instance):
        # delete the descriptor
        delattr(type(instance), self._attr)


def get_introspection_module(namespace):
    """Returns the non proxied module for a namespace"""

    mod = sys.modules["pgi.repository." + namespace]
    return getattr(mod, "_introspection_module", mod)


def load(namespace, module):
    """Takes a namespace e.g. 'Gtk' and a gi module and returns a proxy
    which contains overrides and will fall back to the original module
    if needed.

    The returned module is either a proxy or the passed in module in
    case no overrides exist.
    """

    proxy_type = type(namespace + "ProxyModule", (OverridesProxyModule, ), {})
    proxy = proxy_type(module)
    for prefix in const.PREFIX:
        sys.modules[prefix + "." + namespace] = proxy

    try:
        name = __name__ + "." + namespace
        override_module = __import__(name, fromlist=[""])
    except Exception as err:
        try:
            paths = [os.path.dirname(__file__)]
            fp, pn, desc = imp.find_module(namespace, paths)
        except ImportError:
            # no need for a proxy then, so revert back
            for prefix in const.PREFIX:
                sys.modules[prefix + "." + namespace] = module
            return module
        else:
            if fp:
                fp.close()
            _compat.reraise(ImportError, err, sys.exc_info()[2])
    else:
        # add all objects referenced in __all__ to the original module
        override_vars = vars(override_module)
        override_all = override_vars.get("__all__") or []

        for var in override_all:
            item = override_vars.get(var)
            assert item is not None
            # make sure new classes have a proper __module__
            try:
                if item.__module__.split(".")[-1] == namespace:
                    item.__module__ = namespace
            except AttributeError:
                pass
            setattr(proxy, var, item)

        # Replace deprecated module level attributes with a descriptor
        # which emits a warning when accessed.
        for attr, replacement in _deprecated_attrs.pop(namespace, []):
            try:
                value = getattr(proxy, attr)
            except AttributeError:
                raise AssertionError(
                    "%s was set deprecated but wasn't added to __all__" % attr)
            delattr(proxy, attr)
            deprecated_attr = _DeprecatedAttribute(
                namespace, attr, value, replacement)
            setattr(proxy_type, attr, deprecated_attr)

        return proxy


def override(klass):
    """Takes a override class or function and assigns it dunder arguments
    form the overidden one.
    """

    namespace = klass.__module__.rsplit(".", 1)[-1]
    mod_name = const.PREFIX[-1] + "." + namespace
    module = sys.modules[mod_name]

    if isinstance(klass, types.FunctionType):
        def wrap(wrapped):
            setattr(module, klass.__name__, wrapped)
            return wrapped
        return wrap

    old_klass = klass.__mro__[1]
    name = old_klass.__name__
    klass.__name__ = name
    klass.__module__ = old_klass.__module__

    setattr(module, name, klass)

    return klass


def deprecated(function, instead):
    """Mark a function deprecated so calling it issues a warning"""

    # skip for classes, breaks doc generation
    if not isinstance(function, types.FunctionType):
        return function

    @wraps(function)
    def wrap(*args, **kwargs):
        warnings.warn("Deprecated, use %s instead" % instead,
                      PyGIDeprecationWarning)
        return function(*args, **kwargs)

    return wrap


def deprecated_attr(namespace, attr, replacement):
    """Marks a module level attribute as deprecated. Accessing it will emit
    a PyGIDeprecationWarning warning.

    e.g. for ``deprecated_attr("GObject", "STATUS_FOO", "GLib.Status.FOO")``
    accessing GObject.STATUS_FOO will emit:

        "GObject.STATUS_FOO is deprecated; use GLib.Status.FOO instead"

    :param str namespace:
        The namespace of the override this is called in.
    :param str namespace:
        The attribute name (which gets added to __all__).
    :param str replacement:
        The replacement text which will be included in the warning.
    """

    _deprecated_attrs.setdefault(namespace, []).append((attr, replacement))


def deprecated_init(super_init_func, arg_names, ignore=tuple(),
                    deprecated_aliases={}, deprecated_defaults={},
                    category=PyGIDeprecationWarning,
                    stacklevel=2):
    """Wrapper for deprecating GObject based __init__ methods which specify
    defaults already available or non-standard defaults.

    :param callable super_init_func:
        Initializer to wrap.
    :param list arg_names:
        Ordered argument name list.
    :param list ignore:
        List of argument names to ignore when calling the wrapped function.
        This is useful for function which take a non-standard keyword that is munged elsewhere.
    :param dict deprecated_aliases:
        Dictionary mapping a keyword alias to the actual g_object_newv keyword.
    :param dict deprecated_defaults:
        Dictionary of non-standard defaults that will be used when the
        keyword is not explicitly passed.
    :param Exception category:
        Exception category of the error.
    :param int stacklevel:
        Stack level for the deprecation passed on to warnings.warn
    :returns: Wrapped version of ``super_init_func`` which gives a deprecation
        warning when non-keyword args or aliases are used.
    :rtype: callable
    """
    # We use a list of argument names to maintain order of the arguments
    # being deprecated. This allows calls with positional arguments to
    # continue working but with a deprecation message.
    def new_init(self, *args, **kwargs):
        """Initializer for a GObject based classes with support for property
        sets through the use of explicit keyword arguments.
        """
        # Print warnings for calls with positional arguments.
        if args:
            warnings.warn('Using positional arguments with the GObject constructor has been deprecated. '
                          'Please specify keyword(s) for "%s" or use a class specific constructor. '
                          'See: https://wiki.gnome.org/PyGObject/InitializerDeprecations' %
                          ', '.join(arg_names[:len(args)]),
                          category, stacklevel=stacklevel)
            new_kwargs = dict(zip(arg_names, args))
        else:
            new_kwargs = {}
        new_kwargs.update(kwargs)

        # Print warnings for alias usage and transfer them into the new key.
        aliases_used = []
        for key, alias in deprecated_aliases.items():
            if alias in new_kwargs:
                new_kwargs[key] = new_kwargs.pop(alias)
                aliases_used.append(key)

        if aliases_used:
            warnings.warn('The keyword(s) "%s" have been deprecated in favor of "%s" respectively. '
                          'See: https://wiki.gnome.org/PyGObject/InitializerDeprecations' %
                          (', '.join(deprecated_aliases[k] for k in sorted(aliases_used)),
                           ', '.join(sorted(aliases_used))),
                          category, stacklevel=stacklevel)

        # Print warnings for defaults different than what is already provided by the property
        defaults_used = []
        for key, value in deprecated_defaults.items():
            if key not in new_kwargs:
                new_kwargs[key] = deprecated_defaults[key]
                defaults_used.append(key)

        if defaults_used:
            warnings.warn('Initializer is relying on deprecated non-standard '
                          'defaults. Please update to explicitly use: %s '
                          'See: https://wiki.gnome.org/PyGObject/InitializerDeprecations' %
                          ', '.join('%s=%s' % (k, deprecated_defaults[k]) for k in sorted(defaults_used)),
                          category, stacklevel=stacklevel)

        # Remove keywords that should be ignored.
        for key in ignore:
            if key in new_kwargs:
                new_kwargs.pop(key)

        return super_init_func(self, **new_kwargs)

    return new_init


def strip_boolean_result(method, exc_type=None, exc_str=None, fail_ret=None):
    """Translate method's return value for stripping off success flag.

    There are a lot of methods which return a "success" boolean and have
    several out arguments. Translate such a method to return the out arguments
    on success and None on failure.
    """
    @wraps(method)
    def wrapped(*args, **kwargs):
        ret = method(*args, **kwargs)
        if ret[0]:
            if len(ret) == 2:
                return ret[1]
            else:
                return ret[1:]
        else:
            if exc_type:
                raise exc_type(exc_str or 'call failed')
            return fail_ret
    return wrapped
