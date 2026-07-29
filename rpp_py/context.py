
class ComponentContext:
    """
    A class that represents the context of a plugin in the RPP framework.
    It provides access to various components and services that a plugin may need during its lifecycle.
    """

    def __init__(self, context_impl):
        """
        Initializes the ComponentContext with the given implementation.

        :param context_impl: The underlying implementation of the context.
        """
        self._context_impl = context_impl

    def get_context_impl(self):
        """
        Returns the underlying implementation of the context.

        :return: The context implementation.
        """
        return self._context_impl