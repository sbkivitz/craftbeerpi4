__all__ = ["PropertyType", "Property"]


class PropertyType(object):
    """Base of every property.

    show_if, accepted by every property, names the settings a field depends
    on, so a form asks only for what applies. A dict needs every listed setting
    to hold one of its values; a list of dicts needs any one of them:

        show_if={"Mode": "Date"}
        show_if={"Mode": ["Date", "DateTime"]}
        show_if=[{"Timing": "Ready by"}, {"Campden_Reminder": "Yes"}]

    A blank setting matches "" only, so a field that applies to a setting's
    default should list "" as well. It is a hint to the interface and nothing
    more: a hidden field keeps its value, and the server never refuses a
    setting because of it. None, the default, shows the field always - which
    is how every property written before this existed is displayed.

    display_label, also accepted by every property, is what the form shows in
    place of the label. The label stays the setting's name - the key its value
    is saved under, which a plugin cannot rename without losing every saved
    profile - so "StartTime" can read "Time" on screen. None shows the label.
    """

    show_if = None
    display_label = None


class Property(object):
    class Select(PropertyType):
        """
        Select Property. The user can select value from list set as options parameter
        """

        def __init__(self, label, options, default_value=None, description="",
                     show_if=None, display_label=None):
            """

            :param label:
            :param options:
            :param default_value:
            :param description:
            :param show_if: optional - see PropertyType
            :param display_label: optional - see PropertyType
            """
            PropertyType.__init__(self)
            self.label = label
            self.options = options
            self.default_value = default_value
            self.description = description
            self.show_if = show_if
            self.display_label = display_label

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
            show_if=None,
            display_label=None,
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
            :param show_if: optional - see PropertyType
            :param display_label: optional - see PropertyType
            """
            PropertyType.__init__(self)
            self.label = label
            self.configurable = configurable
            self.default_value = default_value
            self.description = description
            self.show_if = show_if
            self.display_label = display_label

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

        def __init__(self, label, configurable=False, default_value="", description="",
                     show_if=None, display_label=None):
            """

            :param label:
            :param configurable:
            :param default_value:
            :param description:
            :param show_if: optional - see PropertyType
            :param display_label: optional - see PropertyType
            """
            PropertyType.__init__(self)
            self.label = label
            self.configurable = configurable
            self.default_value = default_value
            self.description = description
            self.show_if = show_if
            self.display_label = display_label

    class Date(Text):
        """
        A calendar date, picked with the browser's own date picker.

        The value is text, "YYYY-MM-DD" - what a browser's date input gives
        whatever the brewer's locale shows. Typed free text was the old way, and
        every plugin parsed its own handful of formats. A Text underneath, so
        code that handles text properties handles this one, and an interface
        that predates it shows an ordinary text field.
        """

    class Time(Text):
        """
        A time of day, picked with the browser's own time picker.

        The value is text, 24-hour "HH:MM" - what a browser's time input gives,
        even where it displays 7:00 AM. A Text underneath, like Date.
        """

    class Actor(PropertyType):
        """
        The user select an actor which is available in the system. The value of this variable will be the actor id
        """

        def __init__(self, label, description="", show_if=None, display_label=None):
            """

            :param label:
            :param description:
            :param show_if: optional - see PropertyType
            :param display_label: optional - see PropertyType
            """
            PropertyType.__init__(self)
            self.label = label
            self.configurable = True
            self.description = description
            self.show_if = show_if
            self.display_label = display_label

    class Sensor(PropertyType):
        """
        The user select a sensor which is available in the system. The value of this variable will be the sensor id
        """

        def __init__(self, label, description="", show_if=None, display_label=None):
            """

            :param label:
            :param description:
            :param show_if: optional - see PropertyType
            :param display_label: optional - see PropertyType
            """
            PropertyType.__init__(self)
            self.label = label
            self.configurable = True
            self.description = description
            self.show_if = show_if
            self.display_label = display_label

    class Kettle(PropertyType):
        """
        The user select a kettle which is available in the system. The value of this variable will be the kettle id
        """

        def __init__(self, label, description="", show_if=None, display_label=None):
            """

            :param label:
            :param description:
            :param show_if: optional - see PropertyType
            :param display_label: optional - see PropertyType
            """

            PropertyType.__init__(self)
            self.label = label
            self.configurable = True
            self.description = description
            self.show_if = show_if
            self.display_label = display_label

    class Fermenter(PropertyType):
        """
        The user select a fermenter which is available in the system. The value of this variable will be the fermenter id
        """

        def __init__(self, label, description="", show_if=None, display_label=None):
            """

            :param label:
            :param description:
            :param show_if: optional - see PropertyType
            :param display_label: optional - see PropertyType
            """

            PropertyType.__init__(self)
            self.label = label
            self.configurable = True
            self.description = description
            self.show_if = show_if
            self.display_label = display_label
