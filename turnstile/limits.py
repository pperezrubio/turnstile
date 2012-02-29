# Copyright 2012 Rackspace
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import math
import time

import metatools

from turnstile import utils


# Recognized units and their names and aliases
_units_list = [
    (1, ('second', 'seconds', 'secs', 'sec', 's')),
    (60, ('minute', 'minutes', 'mins', 'min', 'm')),
    (60 * 60, ('hour', 'hours', 'hrs', 'hr', 'h')),
    (60 * 60 * 24, ('day', 'days', 'd')),
    ]


# Build up a mapping of units to names and vice versa
_units_map = {}
for secs, names in _units_list:
    _units_map[secs] = names[0]
    for name in names:
        _units_map[name] = secs


def get_unit_value(name):
    """Given a unit's name, return its value."""

    # Numbers map to numbers
    if isinstance(name, (int, long)):
        return name

    # Only accept strings from here on
    if not isinstance(name, basestring):
        raise TypeError('name must be a string.')

    # Again, numbers map to numbers
    if name.isdigit():
        return int(name)

    # Look it up in the units map
    return _units_map[name.lower()]


def get_unit_name(value):
    """Given a unit's value, return its name."""

    # Return name if we have one, otherwise stringify value
    return _units_map.get(value, str(value))


class DeferLimit(Exception):
    """Exception raised if limit should not be considered."""

    pass


class Bucket(object):
    """
    Represent a "bucket."  A bucket tracks the necessary values for
    application of the leaky bucket algorithm under the control of a
    limit specification.
    """

    attrs = set(['last', 'next', 'level'])
    eps = 0.1

    def __init__(self, db, limit, key, last=None, next=None, level=0.0):
        """
        Initialize a bucket.

        :param db: The database the bucket is in.
        :param limit: The limit associated with this bucket.
        :param key: The key under which this bucket should be stored.
        :param last: The timestamp of the last request.
        :param next: The timestamp of the next permissible request.
        :param level: The current water level in the bucket.
        """

        self.db = db
        self.limit = limit
        self.key = key
        self.last = last
        self.next = next
        self.level = level

    @classmethod
    def hydrate(cls, db, bucket, limit, key):
        """
        Given a key and a bucket dict, as generated by dehydrate(),
        generate an appropriate instance of Bucket.
        """

        return cls(db, limit, key, **bucket)

    def dehydrate(self):
        """Return a dict representing this bucket."""

        # Only concerned about very specific attributes
        result = {}
        for attr in self.attrs:
            result[attr] = getattr(self, attr)

        return result

    def delay(self, params):
        """Determine delay until next request."""

        now = time.time()

        # Initialize last...
        if not self.last:
            self.last = now

        # How much has leaked out?
        leaked = now - self.last

        # Update the last message time
        self.last = now

        # Update the water level
        self.level = max(self.level - leaked, 0)

        # Are we too full?
        difference = self.level + self.limit.cost - self.limit.unit_value
        if difference >= self.eps:
            self.next = now + difference
            return difference

        # OK, raise the water level and set next to an appropriate
        # value
        self.level += self.limit.cost
        self.next = now

        return None

    @property
    def messages(self):
        """Return remaining messages before limiting."""

        return int(math.floor(((self.limit.unit_value - self.level) /
                               self.limit.unit_value) * self.limit.value))

    @property
    def expire(self):
        """Return the estimated expiration time of this bucket."""

        # Round up and convert to an int
        return int(math.ceil(self.last + self.level))


class LimitMeta(metatools.MetaClass):
    """
    Metaclass for limits.
    """

    _registry = {}

    def __new__(mcs, name, bases, namespace):
        """
        Generate a new Limit class.  Adds the full class name to the
        namespace, for the benefit of dehydrate().  Also registers the
        class in the registry, for the benefit of hydrate().
        """

        # Build the full name
        full_name = '%s:%s' % (namespace['__module__'], name)

        # Add it to the namespace
        namespace['_limit_full_name'] = full_name

        # Set up attrs and skip
        namespace.setdefault('attrs', {})
        namespace.setdefault('skip', set())
        for base in mcs.iter_bases(bases):
            mcs.inherit_dict(base, namespace, 'attrs')
            mcs.inherit_set(base, namespace, 'skip')

        # Create the class
        cls = super(LimitMeta, mcs).__new__(mcs, name, bases, namespace)

        # Register the class
        if full_name not in mcs._registry:
            mcs._registry[full_name] = cls

        return cls


class Limit(object):
    """
    Basic limit.  This can be used as an absolute rate limit on a
    given endpoint or set of endpoints.  All other limit classes must
    subclass this class.
    """

    __metaclass__ = LimitMeta

    attrs = dict(
        uri=dict(
            desc=('The URI the limit applies to.  This should be in a syntax '
                  'recognized by Routes, i.e., "/constant/{variable}".  Note '
                  'that this URI may be displayed to the user.  Required.'),
            type=str,
            ),
        value=dict(
            desc=('The permissible number of requests per unit time.  '
                  'Required.'),
            type=int,
            ),
        unit=dict(
            desc=('The unit of time over which the "value" is considered.  '
                  'This may be a string, such as "second", or an integer '
                  'number of seconds, expressed as a string.  Required.'),
            type=str,
            ),
        verbs=dict(
            desc=('The HTTP verbs this limit should apply to.  Optional.  If '
                  'not provided, this limit matches any request to the URI; '
                  'otherwise, only the listed methods match.  Takes a list of '
                  'strings.'),
            type=list,
            subtype=str,
            default=lambda: [],  # Make sure we don't use the *same* list
            xform=lambda verbs: [v.upper() for v in verbs],
            ),
        requirements=dict(
            desc=('A mapping of variable names in the URI to regular '
                  'expressions; may be used to further restrict a given '
                  'variable to a particular string.  This could be used '
                  'to differentiate a request to "/resource/{id}" from '
                  'a request to "/resource/edit".  Optional.'),
            type=dict,
            subtype=str,
            default=lambda: {},  # Make sure we don't use the *same* dict
            ),
        continue_scan=dict(
            desc=('A boolean which signals whether to consider limits '
                  'following this one in the list.  If True (the '
                  'default), the remaining limits are scanned even if '
                  'this limit matches.  May be set to False to skip '
                  'remaining limits.'),
            type=bool,
            default=True,
            ),
        )

    skip = set(['limit'])

    bucket_class = Bucket

    def __init__(self, db, **kwargs):
        """
        Initialize a new limit.

        :param db: The database the limit object is in.
        :param uri: A routes-compatible URI specification.  Parsed
                    keys will be used as part of the cache key.
        :param value: Integer giving number of requests which can be
                      made during a unit of time.
        :param unit: Unit of time over which to limit the number of
                     requests.  May be an integer (either in native
                     Python int or a string representation) or one of
                     the pre-defined units, such as "minute."
        :param verbs: List of HTTP verbs the limit should be
                      considered for.  If empty or not specified, all
                      hits against the specified URI will be limited.
        :param requirements: Dictionary mapping keys in the URI to
                             regular expressions.  This allows the URI
                             to be further restricted during the
                             matching phase.
        :param continue_scan: If True and the limit matches the
                              request (and processing isn't deferred
                              due to filter() raising DeferLimit), the
                              remaining limits will be scanned.  This
                              defaults to True, but may be set to
                              False to inhibit follow-on limits from
                              being applied.
        """

        self.db = db

        # Save the various arguments
        missing = set()
        for attr, desc in self.attrs.items():
            # A value is provided
            if attr in kwargs:
                value = kwargs[attr]

                # Run the transformer, if one was specified
                if 'xform' in desc:
                    value = desc['xform'](value)
            elif 'default' in desc:
                # Use the default value; if it's callable, call it
                value = (desc['default']() if callable(desc['default']) else
                         desc['default'])
            else:
                # Missing attribute
                missing.add(attr)
                continue

            # Save the attribute value
            setattr(self, attr, value)

        # Did we get all the required attributes?
        if missing:
            raise TypeError("Missing required attributes: %s" %
                            ', '.join(sorted(missing)))

    def __repr__(self):
        """
        Return a representation of the limit.
        """

        base = [self._limit_full_name]
        for attr in sorted(self.attrs):
            desc = self.attrs[attr]
            attr_type = desc.get('type', str)

            # Treat lists and dicts specially
            if attr_type == list:
                sublist = [repr(v) for v in getattr(self, attr)]
                value = '[%s]' % ','.join(sublist)
            elif attr_type == dict:
                sublist = ['%s=%r' % (k, v) for k, v in
                           getattr(self, attr).items()]
                value = '{%s}' % ', '.join(sublist)
            else:
                value = repr(getattr(self, attr))

            base.append('%s=%s' % (attr, value))

        return '<%s at 0x%x>' % (' '.join(base), id(self))

    @classmethod
    def hydrate(cls, db, limit):
        """
        Given a limit dict, as generated by dehydrate(), generate an
        appropriate instance of Limit (or a subclass).  If the
        required limit class cannot be found, returns None.
        """

        # Extract the limit name from the keyword arguments
        cls_name = limit.pop('limit_class')

        # Is it in the registry yet?
        if cls_name not in cls._registry:
            try:
                utils.import_class(cls_name)
            except ImportError:
                # If we failed to import, ignore...
                pass

        # Look it up in the registry
        cls = cls._registry.get(cls_name)

        # Instantiate the thing
        return cls(db, **limit) if cls else None

    def dehydrate(self):
        """Return a dict representing this limit."""

        # Only concerned about very specific attributes
        result = dict(limit_class=self._limit_full_name)
        for attr in self.attrs:
            # Using getattr allows the properties to come into play
            result[attr] = getattr(self, attr)

        return result

    def _route(self, mapper):
        """
        Set up the route(s) corresponding to the limit.  This controls
        which limits are checked against the request.

        :param mapper: The routes.Mapper object to add the route to.
        """

        # Build up the keyword arguments to feed to connect()
        kwargs = dict(conditions=dict(function=self._filter))

        # Restrict the verbs
        if self.verbs:
            kwargs['conditions']['method'] = self.verbs

        # Add requirements, if provided
        if self.requirements:
            kwargs['requirements'] = self.requirements

        # Hook to allow subclasses to override arguments to connect()
        self.route(kwargs)

        # Create the route
        mapper.connect(None, self.uri, **kwargs)

    def route(self, route_args):
        """
        Provides a hook by which additional arguments may be added to
        the route.  For most limits, this should not be needed; use
        the filter() method instead.

        :param route_args: A dictionary of keyword arguments that will
                           be passed to routes.Mapper.connect().  This
                           dictionary should be modified in place.
        """

        pass

    def key(self, params):
        """
        Given a set of parameters describing the request, compute a
        key for accessing the corresponding bucket.

        :param params: A dictionary of parameters describing the
                       request; this is likely based on the dictionary
                       from routes.
        """

        # Build up the key in pieces
        parts = [self._limit_full_name]
        parts.extend('%s=%s' % (k, params[k])
                     for k in sorted(params)
                     if k not in self.skip)
        return '/'.join(parts)

    def _filter(self, environ, params):
        """
        Performs final filtering of the request to determine if this
        limit applies.  Returns False if the limit does not apply or
        if the call should not be limited, or True to apply the limit.
        """

        # First, we need to set up any additional params required to
        # get the bucket.  If the DeferLimit exception is thrown, no
        # further processing is performed.
        try:
            additional = self.filter(environ, params) or {}
        except DeferLimit:
            return False

        # Compute the bucket key
        key = self.key(params)

        # Update the parameters...
        params.update(additional)

        def process_bucket(bucket):
            # Determine the delay for the message
            delay = bucket.delay(params)

            return (bucket, delay)

        # Perform a safe fetch and update of the bucket
        bucket, delay = self.db.safe_update(key, self.bucket_class,
                                            process_bucket, self, key)

        # If we found a delay, store the particulars in the
        # environment; this will later be sorted and an error message
        # corresponding to the longest delay returned.
        if delay is not None:
            environ.setdefault('turnstile.delay', [])
            environ['turnstile.delay'].append((delay, self, bucket))

        # Should we continue the route scan?
        return not self.continue_scan

    def filter(self, environ, params):
        """
        Performs final route filtering.  Should add additional
        parameters to the `params` dict that should be used when
        looking up the bucket.  Parameters that should be added to
        params, but which should not be used to look up the bucket,
        may be returned as a dictionary.  If this limit should not be
        applied to this request, raise DeferLimit.

        Note that the Turnstile configuration is available in the
        environment under the "turnstile.config" key.
        """

        pass

    def format(self, status, headers, environ, bucket):
        """
        Formats a response entity.  Returns a tuple of the desired
        status code and the formatted entity.  The default status code
        is passed in, as is a dictionary of headers.

        :param status: The default status code.  Should be returned to
                       the caller, or an alternate selected.  The
                       status code should include both the number and
                       the message, separated by a single space.
        :param headers: A dictionary of headers for the response.
                        Should update the 'Content-Type' header at a
                        minimum.
        :param environ: The WSGI environment for the request.
        :param bucket: The bucket containing the data which caused the
                       delay decision to be made.  This can be used to
                       obtain such information as the next time the
                       request can be made.
        """

        # This is a default response entity, which can be overridden
        # by limit subclasses.
        entity = ("This request was rate-limited.  "
                  "Please retry your request after %s." %
                  time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                time.gmtime(bucket.next)))
        headers['Content-Type'] = 'text/plain'

        return status, entity

    @property
    def value(self):
        """Retrieve the value for this limit."""

        return self._value

    @value.setter
    def value(self, value):
        """Change the value for this limit."""

        if value <= 0:
            raise ValueError("Limit value must be > 0")

        self._value = value

    @property
    def unit(self):
        """Retrieve the name of the unit used for this limit."""

        return get_unit_name(self._unit)

    @unit.setter
    def unit(self, value):
        """
        Change the unit for this limit to the specified unit.  The new
        value may be specified as an integer, a string the indicating
        number of seconds, or one of the recognized unit names.
        """

        self.unit_value = get_unit_value(value)

    @property
    def unit_value(self):
        """
        Retrieve the unit used for this limit as an integer number of
        seconds.
        """

        return self._unit

    @unit_value.setter
    def unit_value(self, value):
        """
        Change the unit used for this limit to the given number of
        seconds.
        """

        if value <= 0:
            raise ValueError("Unit value must be > 0")

        self._unit = int(value)

    @property
    def cost(self):
        """
        Retrieve the amount by which a request increases the water
        level in the bucket.
        """

        return float(self.unit_value) / float(self.value)
