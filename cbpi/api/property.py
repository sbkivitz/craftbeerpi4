__all__ = ["PropertyType", "Property"]


class PropertyType(object):
    pass


class Property(object):
    class Select(PropertyType):
        """
        Select Property. The user can select value from list set as options parameter
        """

        def __init__(self, label, options, default_value=None, description=""):
            """

            :param label:
            :param options:
            :param default_value:
            :param description:
            """
            PropertyType.__init__(self)
            self.label = label
            self.options = options
            self.default_value = default_value
            self.description = description

    class Number(PropertyType):
        """
        The user can set a number value
        """

        def __init__(
            self,
            label,
            configurable=False,
            default_value=None,
            unit="",
            description="",
            min=None,
            max=None,
            step=None,
        ):
            """

            :param label:
            :param configurable:
            :param default_value:
            :param unit:
            :param description:
            :param min: optional lower bound. With max, the interface may offer
                a slider instead of a free-text field.
            :param max: optional upper bound. See min.
            :param step: optional increment for a slider. Defaults to 1.
            """
            PropertyType.__init__(self)
            self.label = label
            self.configurable = configurable
            self.default_value = default_value
            self.description = description

            # `unit` was accepted and then dropped on the floor - every caller
            # that passed one was silently ignored, and nothing displayed it.
            self.unit = unit

            # Bounds are optional and default to None, which is what keeps this
            # backward compatible: a property that declares neither is
            # indistinguishable from one written before these existed, and the
            # interface renders it exactly as it did.
            #
            # A percentage, a duty or an offset has a real range that the plugin
            # author knows and the interface cannot guess. Declaring it lets the
            # interface offer a control suited to the quantity - and lets a
            # brewer drag a duty rather than type it, which matters on a touch
            # screen with wet hands.
            self.min = min
            self.max = max
            self.step = step

    class Text(PropertyType):
        """
        The user can set a text value
        """

        def __init__(self, label, configurable=False, default_value="", description=""):
            """

            :param label:
            :param configurable:
            :param default_value:
            :param description:
            """
            PropertyType.__init__(self)
            self.label = label
            self.configurable = configurable
            self.default_value = default_value
            self.description = description

    class Actor(PropertyType):
        """
        The user select an actor which is available in the system. The value of this variable will be the actor id
        """

        def __init__(self, label, description=""):
            """

            :param label:
            :param description:
            """
            PropertyType.__init__(self)
            self.label = label
            self.configurable = True
            self.description = description

    class Sensor(PropertyType):
        """
        The user select a sensor which is available in the system. The value of this variable will be the sensor id
        """

        def __init__(self, label, description=""):
            """

            :param label:
            :param description:
            """
            PropertyType.__init__(self)
            self.label = label
            self.configurable = True
            self.description = description

    class Kettle(PropertyType):
        """
        The user select a kettle which is available in the system. The value of this variable will be the kettle id
        """

        def __init__(self, label, description=""):
            """

            :param label:
            :param description:
            """

            PropertyType.__init__(self)
            self.label = label
            self.configurable = True
            self.description = description

    class Fermenter(PropertyType):
        """
        The user select a fermenter which is available in the system. The value of this variable will be the fermenter id
        """

        def __init__(self, label, description=""):
            """

            :param label:
            :param description:
            """

            PropertyType.__init__(self)
            self.label = label
            self.configurable = True
            self.description = description
