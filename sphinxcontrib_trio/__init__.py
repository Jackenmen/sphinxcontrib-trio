"""A sphinx extension to help documenting Python code that uses async/await
(or context managers, or abstract methods, or generators, or ...).

This extension takes a somewhat non-traditional approach, though, based on
the observation that function properties like "classmethod", "async",
"abstractmethod" can be mixed and matched, so the the classic sphinx
approach of defining different directives for all of these quickly becomes
cumbersome. Instead, we override the ordinary function & method directives
to add options corresponding to these different properties, and override the
autofunction and automethod directives to sniff for these
properties. Examples:

A function that returns a context manager:

   .. function:: foo(x, y)
      :with: bar

renders in the docs like:

   with foo(x, y) as bar

The 'bar' part is optional. Use :async-with: for an async context
manager. These are also accepted on method, autofunction, and automethod.

An abstract async classmethod:

   .. method:: foo
      :abstractmethod:
      :classmethod:
      :async:

renders like:

   abstractmethod classmethod await foo()

Or since all of these attributes are introspectable, we can get the same
result with:

   .. automethod:: foo

An abstract static decorator:

   .. method:: foo
      :abstractmethod:
      :staticmethod:
      :decorator:

The :decorator: attribute isn't introspectable, but the others
are, so this also works:

   .. automethod:: foo
      :decorator:

and renders like

   abstractmethod staticmethod @foo

"""

from ._version import __version__

from docutils import nodes
from docutils.parsers.rst import directives
from docutils.parsers.rst.states import Body
from sphinx import addnodes
from sphinx import version_info as sphinx_version_info
from sphinx.domains.python import PyFunction
from sphinx.domains.python import PyObject
from sphinx.domains.python import PyMethod, PyClassMethod, PyStaticMethod
from sphinx.ext.autodoc import (
    FunctionDocumenter, MethodDocumenter, ClassLevelDocumenter, Options, ModuleLevelDocumenter
)
from sphinx.util.docstrings import prepare_docstring

import inspect
import re
try:
    from async_generator import isasyncgenfunction
except ImportError:
    from inspect import isasyncgenfunction

CM_CODES = set()
ACM_CODES = set()

from contextlib import contextmanager
CM_CODES.add(contextmanager(None).__code__)  # type: ignore

try:
    from contextlib2 import contextmanager as contextmanager2
except ImportError:
    pass
else:
    CM_CODES.add(contextmanager2(None).__code__)  # type: ignore

try:
    from contextlib import asynccontextmanager
except ImportError:
    pass
else:
    ACM_CODES.add(asynccontextmanager(None).__code__)  # type: ignore

extended_function_option_spec = {
    "async": directives.flag,
    "decorator": directives.flag,
    "with": directives.unchanged,
    "async-with": directives.unchanged,
    "for": directives.unchanged,
    "async-for": directives.unchanged,
}

extended_method_option_spec = {
    **extended_function_option_spec,
    "abstractmethod": directives.flag,
    "staticmethod": directives.flag,
    "classmethod": directives.flag,
    "property": directives.flag,
}

autodoc_option_spec = {
    "no-auto-options": directives.flag,
}

################################################################
# Extending the basic function and method directives
################################################################


class ExtendedCallableMixin(PyObject):  # inherit PyObject to satisfy MyPy
    def needs_arglist(self):
        if "property" in self.options:
            return False
        if ("decorator" in self.options
                or self.objtype in ["decorator", "decoratormethod"]):
            return False
        return True

    # This does *not* override the superclass get_signature_prefix(), because
    # that gets called by the superclass handle_signature(), which then
    # may-or-may-not insert it into the signode (depending on whether or not
    # it returns an empty string). We want to insert the decorator @ after the
    # prefix but before the regular name. If we let the superclass
    # handle_signature() insert the prefix or maybe not, then we can't tell
    # where the @ goes.
    def _get_signature_prefix(self):
        ret = ""
        if "abstractmethod" in self.options:
            ret += "abstractmethod "
        # objtype checks are for backwards compatibility, to support
        #
        #   .. staticmethod::
        #
        # in addition to
        #
        #   .. method::
        #      :staticmethod:
        #
        # it would be nice if there were a central place we could normalize
        # the directive name into the options dict instead of having to check
        # both here at time-of-use, but I don't understand sphinx well enough
        # to do that.
        #
        # Note that this is the code that determines the ordering of the
        # different prefixes.
        if "staticmethod" in self.options or self.objtype == "staticmethod":
            ret += "staticmethod "
        if "classmethod" in self.options or self.objtype == "classmethod":
            ret += "classmethod "
        # if "property" in self.options:
        #     ret += "property "
        if "with" in self.options:
            ret += "with "
        if "async-with" in self.options:
            ret += "async with "
        for for_type, render in [("for", "for"), ("async-for", "async for")]:
            if for_type in self.options:
                name = self.options.get(for_type, "")
                if not name.strip():
                    name = "..."
                ret += "{} {} in ".format(render, name)
        if "async" in self.options:
            ret += "await "
        return ret

    # But we do want to override the superclass get_signature_prefix to stop
    # it from trying to do its own handling of staticmethod and classmethod
    # directives (the legacy ones)
    def get_signature_prefix(self, sig):
        return ""

    def handle_signature(self, sig, signode):
        ret = super().handle_signature(sig, signode)

        # Add the "@" prefix
        if ("decorator" in self.options
                or self.objtype in ["decorator", "decoratormethod"]):
            signode.insert(0, addnodes.desc_addname("@", "@"))

        # Now that the "@" has been taken care of, we can add in the regular
        # prefix.
        prefix = self._get_signature_prefix()
        if prefix:
            signode.insert(0, addnodes.desc_annotation(prefix, prefix))

        # And here's the suffix:
        for optname in ["with", "async-with"]:
            if self.options.get(optname, "").strip():
                # for some reason a regular space here gets stripped, so we
                # use U+00A0 NO-BREAK SPACE
                s = "\u00A0as {}".format(self.options[optname])
                signode += addnodes.desc_annotation(s, s)

        return ret


class ExtendedPyFunction(ExtendedCallableMixin, PyFunction):
    option_spec = {
        **PyFunction.option_spec,
        **extended_function_option_spec,
    }


class ExtendedPyMethod(ExtendedCallableMixin, PyMethod):
    option_spec = {
        **PyMethod.option_spec,
        **extended_method_option_spec,
    }


class ExtendedPyClassMethod(ExtendedCallableMixin, PyClassMethod):
    option_spec = {
        **PyClassMethod.option_spec,
        **extended_method_option_spec,
    }


class ExtendedPyStaticMethod(ExtendedCallableMixin, PyStaticMethod):
    option_spec = {
        **PyStaticMethod.option_spec,
        **extended_method_option_spec,
    }


################################################################
# Autodoc
################################################################

# Our sniffer never reports more than one item from this set. In principle
# it's possible for something to be, say, an async function that returns
# a context manager ("with await foo(): ..."), but it's extremely unusual, and
# OTOH it's very easy for these to get confused when walking the __wrapped__
# chain (e.g. because async_generator converts an async into an async-for, and
# maybe that then gets converted into an async-with by an async version of
# contextlib.contextmanager). So once we see one of these, we stop looking for
# the others.
EXCLUSIVE_OPTIONS = {"async", "for", "async-for", "with", "async-with"}
FIELD_LIST_ITEM_RE = re.compile(Body.patterns['field_marker'])


def sniff_options(obj):
    options = set()
    # We walk the __wrapped__ chain to collect properties.
    while True:
        if getattr(obj, "__isabstractmethod__", False):
            options.add("abstractmethod")
        if isinstance(obj, classmethod):
            options.add("classmethod")
        if isinstance(obj, staticmethod):
            options.add("staticmethod")
        # if isinstance(obj, property):
        #     options.add("property")
        # Only check for these if we haven't seen any of them yet:
        if not (options & EXCLUSIVE_OPTIONS):
            if inspect.iscoroutinefunction(obj):
                options.add("async")
            # in some versions of Python, isgeneratorfunction returns true for
            # coroutines, so we use elif
            elif inspect.isgeneratorfunction(obj):
                options.add("for")
            if isasyncgenfunction(obj):
                options.add("async-for")
            # Some heuristics to detect when something is a context manager
            if getattr(obj, "__code__", None) in CM_CODES:
                options.add("with")
            if getattr(obj, "__returns_contextmanager__", False):
                options.add("with")
            if getattr(obj, "__code__", None) in ACM_CODES:
                options.add("async-with")
            if getattr(obj, "__returns_acontextmanager__", False):
                options.add("async-with")
        if hasattr(obj, "__wrapped__"):
            obj = obj.__wrapped__
        elif hasattr(obj, "__func__"):  # for staticmethod & classmethod
            obj = obj.__func__
        else:
            break

    return options


def update_with_sniffed_options(obj, option_dict):
    if "no-auto-options" in option_dict:
        return
    sniffed = sniff_options(obj)
    for attr in sniffed:
        # Suppose someone has a generator, and they document it as:
        #
        #   .. autofunction:: my_generator
        #      :for: loop_var
        #
        # We don't want to blow away the existing attr["for"] = "loop_var"
        # with our autodetected attr["for"] = None. So we use setdefault.
        option_dict.setdefault(attr, None)


def passthrough_option_lines(self, option_spec):
    sourcename = self.get_sourcename()
    for option in option_spec:
        if option in self.options:
            if self.options.get(option) is not None:
                line = "   :{}: {}".format(option, self.options[option])
            else:
                line = "   :{}:".format(option)
            self.add_line(line, sourcename)
    doc = self.get_doc()
    if not doc:
        return
    docstring, metadata = separate_metadata("\n".join(sum(doc, [])))
    if self.objtype == "method":
        available_options = extended_method_option_spec
    else:
        available_options = extended_function_option_spec
    for option_name, option_value in metadata.items():
        if option_name not in available_options:
            continue
        if option_value:
            line = "   :{}: {}".format(option_name, option_value)
        else:
            line = "   :{}:".format(option_name)
        self.add_line(line, sourcename)


def filter_trio_fields(app, domain, objtype, content):
    """Filter :trio: field from its docstring."""
    # implementation based on:
    # https://github.com/sphinx-doc/sphinx/blob/f127a2ff5d6d86918a5d3ac975e8ab8a24c407d1/sphinx/domains/python.py#L1023-L1035
    if domain != "py":
        return

    for node in content:
        if isinstance(node, nodes.field_list):
            for field in node:
                field_name = field[0].astext().strip()
                if field_name == "trio" or field_name.startswith("trio "):
                    node.remove(field)
                    break


def separate_metadata(s):
    """Separate docstring into metadata and others."""
    # implementation based on:
    # https://github.com/sphinx-doc/sphinx/blob/f127a2ff5d6d86918a5d3ac975e8ab8a24c407d1/sphinx/util/docstrings.py#L23-L49
    in_other_element = False
    metadata: Dict[str, str] = {}
    lines = []

    if not s:
        return s, metadata

    for line in prepare_docstring(s):
        if line.strip() == "":
            in_other_element = False
            lines.append(line)
        else:
            matched = FIELD_LIST_ITEM_RE.match(line)
            if matched and not in_other_element:
                field_name = matched.group()[1:].split(":", 1)[0]
                if field_name.startswith("trio "):
                    name = field_name[5:].strip()
                    metadata[name] = line[matched.end():].strip()
                else:
                    lines.append(line)
            else:
                in_other_element = True
                lines.append(line)

    return "\n".join(lines), metadata


class ExtendedFunctionDocumenter(FunctionDocumenter):
    priority = FunctionDocumenter.priority + 1
    # You can explicitly set the options in case autodetection fails
    option_spec = {
        **FunctionDocumenter.option_spec,
        **extended_function_option_spec,
        **autodoc_option_spec,
    }

    def add_directive_header(self, sig):
        # We can't call super() here, because we want to *skip* executing
        # FunctionDocumenter.add_directive_header, because starting in Sphinx
        # 2.1 it does its own sniffing, which is worse than ours and will
        # break ours. So we jump straight to the superclass.
        ModuleLevelDocumenter.add_directive_header(self, sig)
        passthrough_option_lines(self, extended_function_option_spec)

    def import_object(self):
        ret = super().import_object()
        # autodoc likes to re-use dicts here for some reason (!?!)
        self.options = Options(self.options)
        update_with_sniffed_options(self.object, self.options)
        return ret


class ExtendedMethodDocumenter(MethodDocumenter):
    priority = MethodDocumenter.priority + 1
    # You can explicitly set the options in case autodetection fails
    option_spec = {
        **MethodDocumenter.option_spec,
        **extended_method_option_spec,
        **autodoc_option_spec,
    }

    def add_directive_header(self, sig):
        # We can't call super() here, because we want to *skip* executing
        # FunctionDocumenter.add_directive_header, because starting in Sphinx
        # 2.1 it does its own sniffing, which is worse than ours and will
        # break ours. So we jump straight to the superclass.
        ClassLevelDocumenter.add_directive_header(self, sig)
        passthrough_option_lines(self, extended_method_option_spec)

    def import_object(self):
        # MethodDocumenter overrides import_object to do some sniffing in
        # addition to just importing. But we do our own sniffing and just want
        # the import, so we un-override it.
        ret = ClassLevelDocumenter.import_object(self)
        # Use 'inspect.getattr_static' to properly detect class or static methods.
        # This also resolves the MRO entries for subclasses.
        obj = inspect.getattr_static(self.parent, self.object_name)
        # autodoc likes to re-use dicts here for some reason (!?!)
        self.options = Options(self.options)
        update_with_sniffed_options(obj, self.options)
        # Replicate the special ordering hacks in
        # MethodDocumenter.import_object
        if "classmethod" in self.options or "staticmethod" in self.options:
            self.member_order -= 1
        return ret

################################################################
# Register everything
################################################################


def setup(app):
    app.add_directive_to_domain('py', 'function', ExtendedPyFunction)
    app.add_directive_to_domain('py', 'method', ExtendedPyMethod)
    app.add_directive_to_domain('py', 'classmethod', ExtendedPyClassMethod)
    app.add_directive_to_domain('py', 'staticmethod', ExtendedPyStaticMethod)
    app.add_directive_to_domain('py', 'decorator', ExtendedPyFunction)
    app.add_directive_to_domain('py', 'decoratormethod', ExtendedPyMethod)

    # Make sure sphinx.ext.autodoc is loaded before we try to mess with it.
    app.setup_extension("sphinx.ext.autodoc")
    # We're overriding these on purpose, so disable the warning about it
    del directives._directives["autofunction"]
    del directives._directives["automethod"]
    app.add_autodocumenter(ExtendedFunctionDocumenter)
    app.add_autodocumenter(ExtendedMethodDocumenter)
    if sphinx_version_info >= (2, 4):
        app.connect("object-description-transform", filter_trio_fields)

    return {'version': __version__, 'parallel_read_safe': True}
