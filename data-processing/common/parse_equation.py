import re
from dataclasses import dataclass
from enum import Enum
from typing import Callable, List, Optional, Tuple, Union, cast

from bs4 import BeautifulSoup, NavigableString, Tag

from common.types import Token

KATEX_ERROR_COLOR = "#ffffff"
"""
KaTeX error color is set to white because this is a color where we'll minimize the chance of
misdetecting colored equations as errors---anything that's set to 'white' in a paper would be
invisible and we wouldn't want to detect it anyway.
"""


class NodeType(Enum):
    IDENTIFIER = "identifier"
    FUNCTION = "function"
    LEFT_PARENS = "left-parens"
    RIGHT_PARENS = "right-parens"
    DEFINITION_OPERATOR = "definition-operator"
    OPERATOR = "operator"

    def __repr__(self) -> str:
        return f"{self.__class__.__name__, self.name}"


@dataclass
class Node:
    " Node for a parse tree for an equation. "

    type_: NodeType
    " A tag describing the role of the node in the equation. "

    element: Tag
    " BeautifulSoup element. Use 'str(node.soup)' to see the cleaned MathML for this node. "

    children: List["Node"]
    " List of all nodes that are a child of this one. "

    start: int
    " Offset of the first character this node was parsed from in the equation. "

    end: int
    " Offset of the last character this node was parsed from in the equation. "

    tokens: List[Token]
    """
    List of all tokens that belong to this symbol. It's tokens in this list that will be colorized
    to determine the bounding box of the symbol.
    """

    defined: Optional[bool] = None
    """
    Whether this node is being defined (i.e., whether it is the top-level symbol) on the left side
    of an equation with an equality. Should only be set to true for nodes that
    corresponds to symbols (e.g., identifiers, functions).
    """

    @property
    def is_symbol(self) -> bool:
        """
        Whether this node should be considered a 'symbol', i.e., an entity in an equation that
        has meaning and needs an explanation.
        """
        return self.type_ in [NodeType.IDENTIFIER, NodeType.FUNCTION]

    @property
    def child_symbols(self) -> List["Node"]:
        """
        Get all child symbols of this node, excluding nodes that serve a syntactic role
        used mostly just for parsing, like parentheses.
        """
        return list(filter(lambda c: c.is_symbol, self.children))

    @property
    def contains_affix_token(self) -> bool:
        """
        Whether this node contains any affix tokens (e.g., hats, arrows). If it doesn't, then
        the location of this node can be determined from the location of all its atom tokens.
        """
        return any([t.type_ == "affix" for t in self.tokens])


def parse_equation(mathml: str) -> List[Node]:
    """
    Extract a list of all symbols from a MathML. Guaranteed to return symbols in the same order
    every time---specifically, in the order visited in breadth-first search.
    """

    soup = BeautifulSoup(mathml, "lxml")

    # Parse the MathML equation, extracting top-level symbols.
    top_level_symbols = parse_element(soup.body).symbols

    # Build a list of all symbols found with a breadth-first search over the symbol tree.
    all_symbols = []
    symbols_to_visit = top_level_symbols
    while len(symbols_to_visit) > 0:
        symbol = symbols_to_visit.pop(0)
        all_symbols.append(symbol)
        symbols_to_visit.extend(symbol.child_symbols)

    return all_symbols


@dataclass(frozen=True)
class ParseResult:

    nodes: List[Node]
    """
    Top-level symbol nodes found by parsing an element. These are nodes that can be considered
    "child symbols", if you are parsing the child tag of the parent.
    """

    element: Optional[Tag]
    """
    A cleaned up BeautifulSoup element of the parsed entity (i.e. with S2 metadata tags removed
    and with consecutive identifier characters merged.). Will be None if no element could be
    parsed from the input element.
    """

    tokens: List[Token]
    """
    List of all tokens found in the subtree starting at the parsed element.
    """

    @property
    def symbols(self) -> List[Node]:
        " Get a list of all top-level symbol nodes parsed from the MathML. "
        return [n for n in self.nodes if n.is_symbol]


def walk_postorder(element: Tag, func: Callable[[Tag], None]) -> None:
    for child in element.children:
        if isinstance(child, Tag):
            walk_postorder(child, func)
    func(element)


def repair_operator_tags(element: Tag) -> None:
    """
    Some elements that are frequently operators (like dots) are parsed by KaTeX as identifiers
    instead of operators. This function changes those identifiers into operators.
    """

    if element.name != "mi":
        return

    if element.text in ["∀", "∃", "|", "∥", "."]:
        operator = clone_element(element)
        operator.name = "mo"
        element.replace_with(operator)


def make_derivatives_into_operators(element: Tag) -> None:
    """
    Transform symbols like 'd's and 'δ's that are probably used as signs for derivatives
    into operators, rather than identifiers.
    """

    DERIVATIVE_GLYPHS = ["d", "δ", "∂"]

    if element.name != "mrow":
        return

    elements = [e for e in element.children if isinstance(e, Tag)]
    for i, e in enumerate(elements):
        is_derivative_symbol = (
            # Is the glyph a derivative sign?
            e.name == "mi"
            and e.text in DERIVATIVE_GLYPHS
            # Is the next element a symbol?
            and (i < len(elements) - 1 and _is_identifier(elements[i + 1]))
            # Is the element after that either not a symbol, or another derivative sign?
            and (
                i == len(elements) - 2
                or not _is_identifier(elements[i + 2])
                or elements[i + 2].text in DERIVATIVE_GLYPHS
            )
        )
        if is_derivative_symbol:
            derivative_operator = clone_element(e)
            derivative_operator.name = "mo"
            e.replace_with(derivative_operator)


def remove_empty_strings(element: Tag) -> None:
    for child in element.children:
        if isinstance(child, NavigableString) and child.isspace():
            child.extract()


def merge_row_elements(element: Tag) -> None:
    """
    If an element is an 'mrow' produced by KaTeX, its children are probably needlessly fragmented. For
    instance, the word 'true' will contain four '<mi>' elements, one for 't', 'r', 'u', and 'e'
    each. Merge such elements into single elements.
    """

    if element.name != "mrow":
        return

    elements = [e for e in element.children if isinstance(e, Tag)]
    merger = MathMlElementMerger()
    merged = merger.merge(elements)

    # If the 'mrow' only contains one element after its children are merged, simplify the
    # MathML tree replacing this node with its merged child.
    if len(merged) == 1:
        element.replace_with(merged[0])
    else:
        for e in elements:
            e.extract()
        for m in merged:
            element.append(m)


def clean_equation_document(root: Tag) -> Tag:
    """
    This method does not destroy the tree passed in as an argument. Rather, it clones the tree,
    cleans the cloned tree, and then returns it.
    """

    root_clone = clone_element(root)

    # As the root may be replaced by another element by the cleaning functions below, it must be
    # nested under a parent element so that the BeautifulSoup 'replace_with' method can be used.
    parent = BeautifulSoup("<div></div>", "lxml").div
    parent.append(root_clone)

    # Perform a series of cleaning operations on the element.
    walk_postorder(parent, remove_empty_strings)
    walk_postorder(parent, make_derivatives_into_operators)
    walk_postorder(parent, repair_operator_tags)
    walk_postorder(parent, merge_row_elements)

    return list(parent.children)[0]


def parse_element(element: Tag) -> ParseResult:
    """
    Extract symbol nodes from an element in a BeautifulSoup MathML parse tree. This function
    recursively visits child elements in the BeautifulSoup parse tree, creating symbols for
    each of the element's descendants. The symbol returned will have one descendant for each
    symbol found during the recursive parse.
    """

    cleaned = clean_equation_document(element)
    parse_result = _parse_element(cleaned)

    # Remove custom S2 annotations that were used for parsing, but which do not belong
    # in a normalized representation of the element.
    if parse_result.element:
        walk_postorder(parse_result.element, _remove_s2_annotations)

    node_queue = list(parse_result.nodes)
    while node_queue:
        node = node_queue.pop()
        _remove_s2_annotations(node.element)
        node_queue.extend(node.children)

    return parse_result


def _parse_element(element: Tag) -> ParseResult:
    """
    Not meant to be called directly. Assumes a normalized form of the element and its children.
    Instead, called 'parse_element', which normalizes the element first.
    """

    # Detect if this tag represents a KaTeX parse error. If so, return nothing.
    if _is_error_element(element):
        return ParseResult([], None, [])

    # Parse each child element. As of BeautifulSoup version 4.8.2, the 'element.children' property
    # ensures that children are visited in order.
    children: List[Node] = []
    tokens: List[Token] = []

    for child in element.children:
        if isinstance(child, Tag):

            # Parse symbols from the child.
            child_parse_result = _parse_element(child)
            children.extend(child_parse_result.nodes)
            tokens.extend(child_parse_result.tokens)

    # Attempt to parse nodes of specific types from the current element. Node type-specific
    # parsers are responsible returning a parse resul. Type-specific parsers may take in:
    # * the element, if they need to extract tokens from the element;
    # * the list of all tokens parsed in the parses of child elements, because some tokens will have been found that
    #   don't have a corresponding node in 'children' (for instance, numbers, etc.).
    original_element = element
    identifier = parse_identifier(element, children, tokens)
    if identifier is not None:
        return identifier
    functions = parse_functions(element, children, tokens)
    if functions is not None:
        children = functions.nodes
    parens = parse_parens(element)
    if parens is not None:
        return parens
    definition_operator = parse_definition_operator(element)
    if definition_operator is not None:
        return definition_operator

    # Now that the sequence of children has been transformed by sanitizers and parsers, check
    # to see if any of the children in the sequence have been defined.
    for i, c in enumerate(children):
        if c.type_ == NodeType.DEFINITION_OPERATOR and i >= 1:
            previous_child = children[i - 1]
            if previous_child.is_symbol and not _appears_in_operator_argument(
                original_element
            ):
                previous_child.defined = True

    # If a specific type of node can't be parsed, then return a generic type of node,
    # comprising of all the children and tokens found in this element.
    tokens.extend(_extract_tokens(element))
    return ParseResult(children, element, tokens)


def _is_identifier(element: Tag) -> bool:
    " Determine whether an element represents identifier. "

    if element.name == "mi":
        # Exclude dots, which are parsed as identifiers.
        if element.text in ["."]:
            return False
        return True
    # Composite symbols like subscripts and superscripts must have multiple children to be
    # considered a symbol, and their base (i.e., first argument) must be a symbol. In most cases,
    # this means that the base is an identifier. This rules out operators like summations.
    if element.name in ["msubsup", "msub", "msup"]:
        child_elements = list(element.children)
        return (
            len(child_elements) >= 1
            and isinstance(child_elements[0], Tag)
            and _is_identifier(child_elements[0])
        )
    if element.name == "mtext" and re.match(r"\w+", element.text):
        return True
    if element.name == "mover":
        child_elements = list(element.children)
        return (
            len(child_elements) == 2
            and isinstance(child_elements[0], Tag)
            and _is_identifier(child_elements[0])
        )

    return False


def parse_identifier(
    element: Tag, children: List[Node], tokens: List[Token]
) -> Optional[ParseResult]:
    " Attempt to parse an element as an identifier. "

    if _is_identifier(element) and _has_s2_offset_annotations(element):

        tokens = list(tokens)
        tokens.extend(_extract_tokens(element))

        node = Node(
            NodeType.IDENTIFIER,
            element,
            children,
            int(element.attrs["s2:start"]),
            int(element.attrs["s2:end"]),
            tokens,
        )
        return ParseResult([node], element, tokens)

    return None


def parse_functions(
    element: Tag, children: List[Node], tokens: List[Token]
) -> Optional[ParseResult]:
    """
    Attempt to parse an row of children into functions. Because this function processes rows of
    elements ('mrow'), it might find multiple functions, for example if two functions are
    multiplied by each other (e.g., 'f(x)g(x)').
    """

    if not element.name == "mrow":
        return None

    new_children = list(children)
    function_spans: List[Tuple[int, int]] = []
    span_start = -1
    span_end = -1
    parens_depth = 0

    # Detect ranges of children than should be combined into functions.
    for i, child in enumerate(children):

        if parens_depth == 0:
            # Each identifier found in a row could be an identifier for a function,
            # so the last potential function identifier is saved.
            if child.type_ is NodeType.IDENTIFIER:
                span_start = i
                continue
            # Start parsing a function when a left parentheses if found.
            elif child.type_ is NodeType.LEFT_PARENS:
                if span_start != -1:
                    parens_depth += 1
                    continue
            # If this is not an identifier or a left parens, don't consider the
            # current child as a starting point for making a function.
            else:
                span_start = -1

        if parens_depth > 0:
            if child.type_ is NodeType.RIGHT_PARENS:
                parens_depth -= 1
                if parens_depth == 0:
                    span_end = i
                    function_spans.append((span_start, span_end))
                    span_start = -1
                    span_end = -1

    # Nest children that appear to be parts of function in new function nodes.
    function_created = False
    for span_start, span_end in reversed(function_spans):
        if span_start == -1 or span_end == -1:
            continue

        func_children = children[span_start : span_end + 1]
        func_tokens = [t for c in func_children for t in c.tokens]

        # Create synthetic MathML row for the function.
        func_element = create_empty_element_copy(element)
        add_elements = False
        for child_element in element.children:
            if child_element is func_children[0].element:
                add_elements = True
            if add_elements:
                func_element.append(clone_element(child_element))
            if child_element is func_children[-1].element:
                add_elements = False
                break

        func_node = Node(
            NodeType.FUNCTION,
            func_element,
            func_children,
            func_children[0].start,
            func_children[-1].end,
            func_tokens,
        )
        new_children = (
            new_children[:span_start] + [func_node] + new_children[span_end + 1 :]
        )
        function_created = True

    if function_created:
        return ParseResult(new_children, element, tokens)
    return None


def parse_parens(element: Tag) -> Optional[ParseResult]:
    if (
        element.name == "mo"
        and element.text in ["(", ")"]
        and _has_s2_offset_annotations(element)
    ):

        tokens = _extract_tokens(element)

        node = Node(
            type_=NodeType.LEFT_PARENS
            if element.text == "("
            else NodeType.RIGHT_PARENS,
            element=element,
            children=[],
            start=int(element.attrs["s2:start"]),
            end=int(element.attrs["s2:end"]),
            tokens=tokens,
        )
        return ParseResult([node], element, tokens)

    return None


def parse_definition_operator(element: Tag) -> Optional[ParseResult]:
    EQUATION_SIGNS = ["=", "≈", "≥", "≤", "<", ">", "∈", "∼", "≜"]
    if (
        element.name != "mo"
        or element.text not in EQUATION_SIGNS
        or not _has_s2_offset_annotations(element)
    ):
        return None

    tokens = _extract_tokens(element)
    node = Node(
        type_=NodeType.DEFINITION_OPERATOR,
        element=element,
        children=[],
        start=int(element.attrs["s2:start"]),
        end=int(element.attrs["s2:end"]),
        tokens=tokens,
    )
    return ParseResult([node], element, tokens)


def _extract_tokens(element: Tag) -> List[Token]:
    """
    Get the tokens defined in this element. Tokens are characters or spans of text that
    make up symbols. There should be a token returned for each glyph in a symbol that needs
    to be detected separately (e.g., a symbol's base and its subscript are different tokens).

    Tokens are only found in low-level elements like "<mi>" and "<mn>". This function will
    not find tokens in higher-level nodes that solely group other low-level elements (like
    "<mrow>" and "<msub>").
    """

    tokens = []

    if _is_atomic_token(element):
        tokens.append(
            Token(
                # Convert text to a primitive type. 'element.string' is a NavigableString,
                # which causes recursion errors when serialized.
                text=str(element.string),
                start=int(element["s2:start"]),
                end=int(element["s2:end"]),
                type_="atom",
            )
        )
    elif _is_affix_token(element):
        tokens.append(
            Token(
                text=str(element.string),
                start=int(element["s2:start"]),
                end=int(element["s2:end"]),
                type_="affix",
            )
        )

    return tokens


def _is_atomic_token(element: Tag) -> bool:
    """
    Determine whether an element contains a token that can stand on its own in TeX (i.e., which
    can be visually styled by wrapping the span of text in TeX macros).
    """

    if not _has_s2_offset_annotations(element):
        return False

    # Some tags always contain tokens.
    TOKEN_TAGS = ["mn", "mi"]
    if element.name in TOKEN_TAGS:
        return True
    # The prime symbol (e.g., "x'").
    if element.name == "mo" and element.text in ["′", "'"]:
        return True
    # Parentheses, for functions.
    if element.name == "mo" and element.text in ["(", ")"]:
        return True
    # Text spans consisting of only a single word.
    if element.name == "mtext" and re.match(r"\w+", element.text):
        return True

    return False


def _is_affix_token(element: Tag) -> bool:
    """
    Determine whether an element corresponds to an affix token (e.g., a bar above an
    identifier that was generated by a TeX macro).
    """

    if (
        element.name != "mo"
        or element.parent is None
        or not isinstance(element.parent, Tag)
    ):
        return False

    parent = cast(Tag, element.parent)
    return (
        bool(parent.name == "mover")
        and bool(element.find_previous("mi"))
        and (parent.attrs.get("accent") == "true")
    )


def _appears_in_operator_argument(element: Tag) -> bool:
    """
    Detect whether this element appears in an argument to an operator. Used to determine, for instance,
    whether a definition appears in the argument of a summation.
    """
    parent = element
    while parent is not None:
        parent_children = [c for c in parent.children if isinstance(c, Tag)]
        if (
            parent.name in ["msubsup", "msub", "msup"]
            and len(parent_children) > 0
            and parent_children[0].name == "mo"
        ):
            return True
        parent = parent.parent

    return False


def _is_error_element(element: Tag) -> bool:
    " Detect whether a BeautifulSoup tag represents a KaTeX parse error. "

    return (element.name == "mstyle") and bool(
        element.attrs.get("mathcolor") == KATEX_ERROR_COLOR
    )


def _remove_s2_annotations(element: Tag) -> None:
    " Remove S2 metadata tags from a BeautifulSoup node. "
    if hasattr(element, "attrs"):
        for key in list(element.attrs.keys()):
            if key.startswith("s2:"):
                del element.attrs[key]


def create_empty_element_copy(element: Tag) -> Tag:
    """
    Create a new BeautifulSoup element that will be a cleaned (empty) clone of the element.
    Create the clone by creating a new tag, rather than using 'copy.copy', because edits made
    to copies of elements will modify the contents of the original element, which may be in
    use by other parts of the code.
    """
    clone = create_element(element.name)
    for key, value in element.attrs.items():
        clone.attrs[key] = value

    return clone


def clone_element(element: Union[Tag, NavigableString]) -> Union[Tag, NavigableString]:
    " Create a deep copy of an element from a BeautifulSoup tree. "
    if isinstance(element, Tag):
        new_element = create_empty_element_copy(element)
        for child in element.children:
            new_element.append(clone_element(child))
        return new_element

    return NavigableString(str(element))


def create_element(tag_name: str) -> Tag:
    " Create a BeautifulSoup tag with the given tag_name. "

    # A dummy BeautifulSoup object is created to access to the 'new_tag' function.
    return BeautifulSoup("", "lxml").new_tag(tag_name)


class MathMlElementMerger:
    def merge(self, elements: List[Tag]) -> List[Tag]:
        """
        Merge consecutive  elements in a list of elements. Do not modify the input list of elements, rather
        return a new list of elements.
        """
        self.merged: List[Tag] = []  # pylint: disable=attribute-defined-outside-init
        self.to_merge: List[Tag] = []  # pylint: disable=attribute-defined-outside-init

        # Main loop: iterate over elements, merging when possible.
        for e in elements:
            # Skip over whitespace.
            if isinstance(e, str) and e.isspace():
                continue
            # If an element is a mergeable type of element...
            if self._is_mergeable_type(e):
                # Merge with prior elements if you can. Otherwise, merge the prior elements, now that
                # we know there are no more elements to merge with them.
                if not self._can_merge_with_prior_elements(e):
                    self._merge_prior_elements()
                self.to_merge.append(e)
            # When an element can't be merged, merge all prior elements, and add this element
            # to the list of elements without changing it.
            else:
                self._merge_prior_elements()
                self.merged.append(e)

        # If there elements still waiting to be merged, merge them.
        if len(self.to_merge) > 0:
            self._merge_prior_elements()

        return self.merged

    def _is_mergeable_type(self, element: Tag) -> bool:
        " Determine if a element is a type that is mergeable with other elements. "
        MERGEABLE_TOKEN_TAGS = ["mn", "mi"]
        return element.name in MERGEABLE_TOKEN_TAGS and _has_s2_offset_annotations(
            element
        )

    def _can_merge_with_prior_elements(self, element: Tag) -> bool:
        """
        Determine whether an element can be merged into the list of prior elements. It is
        assumed that you have already called _is_mergeable_type on the element to check if it
        can be merged before calling this method.
        """

        # If there are no element to merge with, then the element will merge with an empty list.
        if len(self.to_merge) == 0:
            return True

        # For two elements to be merged together, one must follow the other without spaces.
        last_element = self.to_merge[-1]
        element_start = element.attrs["s2:start"]
        last_element_end = last_element.attrs["s2:end"]
        if not element_start == last_element_end:
            return False

        # Here come the context-sensitive rules:
        # 1. Letters can be merged into any sequence of elements before them that starts with a
        #    a letter. This allows tokens to be merged into (target letter is shown in
        #    <angled brackets> identifiers like "r2<d>2", but not constant multiplications like
        #   "4<x>", which should be split into two symbols.
        if element.name == "mi":
            return bool(self.to_merge[0].name == "mi")
        # 2. Numbers can be merged into letters before them, adding to the identifier.
        # 3. Numbers can be merged into numbers before them, extending an identifier, or making
        #    a number with multiple digits.
        if element.name == "mn":
            return True

        return False

    def _merge_prior_elements(self) -> None:
        """
        Merge all of the identifiers seen up to this point into a new element, and add that element to
        the list of all merged elements.
        """
        if len(self.to_merge) == 0:
            return

        # Determine the new tag type based on the tags that will be merged. For now, we can assume
        # that it's the same as the first type of element that will be merged.
        tag_name = self.to_merge[0].name

        # Create a new BeautifulSoup object with the contents of all identifiers appended together.
        new_text = "".join([n.string for n in self.to_merge])
        element = create_element(tag_name)
        element.string = new_text
        element.attrs["s2:start"] = self.to_merge[0].attrs["s2:start"]
        element.attrs["s2:end"] = self.to_merge[-1].attrs["s2:end"]

        # An identifier should have no children in MathML.
        self.merged.append(element)

        # Now that the prior elements have been merged, clear the list.
        self.to_merge = []  # pylint: disable=attribute-defined-outside-init


def _has_s2_offset_annotations(tag: BeautifulSoup) -> bool:
    return "s2:start" in tag.attrs and "s2:end" in tag.attrs
