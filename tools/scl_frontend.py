"""Located SCL lexer/AST/parser. Unsupported syntax is an error, never skipped."""
from __future__ import annotations
from dataclasses import dataclass
import re
from scl_audit import mask_noncode


class CompileError(ValueError):
    pass


@dataclass(frozen=True)
class Token:
    text: str
    file: str
    line: int
    column: int
    kind: str = "symbol"

    def fail(self, message):
        raise CompileError(f"{self.file}:{self.line}:{self.column}: {message}")


@dataclass
class Node:
    op: str
    args: tuple
    token: Token


@dataclass
class TypeSpec:
    kind: str
    args: tuple
    token: Token


@dataclass
class Decl:
    name: str
    spec: TypeSpec
    initial: list[Node]
    token: Token
    overlay: str | None = None


@dataclass
class Function:
    name: str
    locals: list[Decl]
    body: list[Node]
    token: Token
    result: str = "VOID"


TOKEN = re.compile(
    r"(?P<space>\s+)|(?P<string>'(?:''|[^'])*')|(?P<quoted>\"(?:\"\"|[^\"])*\")|"
    r"(?P<number>(?:16#[0-9a-fA-F_]+|8#[0-7_]+|2#[01_]+|\d[\d_]*(?:\.(?!\.)\d[\d_]*)?(?:[eE][+-]?\d+)?))|"
    r"(?P<id>[A-Za-z_]\w*)|(?P<symbol>:=|=>|<=|>=|<>|\*\*|\.\.|[;:,.()\[\]+*/=<>\-#])"
)


def lex(source: str, filename: str) -> list[Token]:
    try:
        code = mask_noncode(source, strings=False)
    except ValueError as error:
        raise CompileError(f"{filename}: {error}") from error
    tokens = []
    pos, line, column = 0, 1, 1
    while pos < len(code):
        match = TOKEN.match(code, pos)
        if not match:
            Token(code[pos], filename, line, column).fail("unsupported character " + repr(code[pos]))
        text, kind = match[0], match.lastgroup
        if kind != "space":
            if kind == "quoted":
                text, kind = text[1:-1].replace('""', '"'), "id"
            tokens.append(Token(text, filename, line, column, kind))
        raw = match[0]
        if "\n" in raw:
            line += raw.count("\n")
            column = len(raw.rsplit("\n", 1)[1]) + 1
        else:
            column += len(raw)
        pos = match.end()
    tokens.append(Token("<EOF>", filename, line, column))
    return tokens


PRECEDENCE = {"OR": 10, "XOR": 20, "AND": 30, "=": 35, "<>": 35,
              "<": 40, ">": 40, "<=": 40, ">=": 40, "+": 50, "-": 50,
              "*": 60, "/": 60, "DIV": 60, "MOD": 60}


class Parser:
    def __init__(self, source, filename="<source>"):
        self.tokens = lex(source, filename)
        self.i = 0

    @property
    def token(self):
        return self.tokens[self.i]

    def is_(self, value):
        return self.token.text.upper() == value.upper()

    def take(self):
        token = self.token
        if token.text != "<EOF>":
            self.i += 1
        return token

    def accept(self, value):
        if self.is_(value):
            return self.take()
        return None

    def expect(self, value):
        if not self.is_(value):
            self.token.fail(f"expected {value}, found {self.token.text}")
        return self.take()

    def identifier(self):
        if self.token.kind != "id":
            self.token.fail("expected identifier")
        return self.take()

    def expression(self, minimum=0):
        t = self.take()
        if t.text.upper() in {"NOT", "+", "-"}:
            left = Node("unary", (t.text.upper(), self.expression(70)), t)
        elif t.text == "(":
            left = self.expression()
            self.expect(")")
        elif t.kind == "number" or t.text.upper() in {"TRUE", "FALSE"}:
            left = Node("literal", (t.text,), t)
        elif t.kind == "string":
            left = Node("string", (t.text[1:-1].replace("''", "'"),), t)
        elif t.kind == "id":
            if self.accept("#"):
                left = Node("typed", (t.text.upper(), self.expression(70)), t)
            elif self.accept("("):
                args = []
                if not self.is_(")"):
                    while True:
                        label = None
                        if self.token.kind == "id" and self.tokens[self.i + 1].text in {":=", "=>"}:
                            label = (self.take().text, self.take().text)
                        args.append((label, self.expression()))
                        if not self.accept(","):
                            break
                self.expect(")")
                left = Node("call", (t.text, args), t)
            else:
                selectors = []
                while True:
                    if self.accept("."):
                        selectors.append(("field", self.identifier().text))
                    elif self.accept("["):
                        while True:
                            selectors.append(("index", self.expression()))
                            if not self.accept(","):
                                break
                        self.expect("]")
                    else:
                        break
                left = Node("ref", (t.text, selectors), t)
        else:
            t.fail(f"expected expression, found {t.text}")
        while PRECEDENCE.get(self.token.text.upper(), -1) >= minimum:
            operator = self.take()
            right = self.expression(PRECEDENCE[operator.text.upper()] + 1)
            left = Node("binary", (operator.text.upper(), left, right), operator)
        return left

    def type_spec(self):
        t = self.take()
        name = t.text.upper()
        if name == "ARRAY":
            self.expect("[")
            bounds = []
            while True:
                low = self.expression()
                self.expect("..")
                high = self.expression()
                bounds.append((low, high))
                if not self.accept(","):
                    break
            self.expect("]")
            self.expect("OF")
            return TypeSpec("array", (bounds, self.type_spec()), t)
        if name == "STRUCT":
            declarations = self.declarations("END_STRUCT")
            self.expect("END_STRUCT")
            return TypeSpec("struct", (declarations,), t)
        if name == "STRING":
            length = None
            if self.accept("["):
                length = self.expression()
                self.expect("]")
            return TypeSpec("string", (length,), t)
        if t.kind != "id":
            t.fail("expected type name")
        return TypeSpec("named", (name,), t)

    def declarations(self, terminator):
        result = []
        while not self.is_(terminator):
            names = [self.identifier()]
            while self.accept(","):
                names.append(self.identifier())
            overlay = self.identifier().text if self.accept("AT") else None
            self.expect(":")
            spec = self.type_spec()
            initial = []
            if self.accept(":="):
                initial.append(self.expression())
                while self.accept(","):
                    initial.append(self.expression())
            self.expect(";")
            result.extend(Decl(t.text, spec, initial, t, overlay) for t in names)
        return result

    def types(self):
        result = {}
        while not self.is_("<EOF>"):
            self.expect("TYPE")
            name = self.identifier()
            self.expect(":")
            spec = self.type_spec()
            self.expect(";")
            self.expect("END_TYPE")
            if name.text.upper() in result:
                name.fail("duplicate type")
            result[name.text.upper()] = spec
        return result

    def data_blocks(self):
        result = {}
        while not self.is_("<EOF>"):
            self.expect("DATA_BLOCK")
            name = self.identifier()
            spec = self.type_spec()
            self.expect(";")
            self.expect("BEGIN")
            body = self.statements({"END_DATA_BLOCK"})
            self.expect("END_DATA_BLOCK")
            if name.text.upper() in result:
                name.fail("duplicate data block")
            result[name.text.upper()] = (name, spec, body)
        return result

    def function(self):
        t = self.take()
        if t.text.upper() not in {"FUNCTION", "ORGANIZATION_BLOCK"}:
            t.fail("only FUNCTION and ORGANIZATION_BLOCK are supported")
        name = self.identifier()
        result = "VOID"
        if t.text.upper() == "FUNCTION":
            self.expect(":")
            result = self.identifier().text.upper()
        locals_ = []
        if self.accept("VAR_TEMP"):
            locals_ = self.declarations("END_VAR")
            self.expect("END_VAR")
            self.accept(";")
        self.expect("BEGIN")
        end = "END_" + t.text.upper()
        body = self.statements({end})
        self.expect(end)
        self.expect("<EOF>")
        return Function(name.text, locals_, body, name, result)

    def statements(self, stops, case=False):
        body = []
        while self.token.text.upper() not in stops:
            if case and (self.token.kind == "number" or self.is_("-")):
                break
            if self.accept(";"):
                continue
            body.append(self.statement())
        return body

    def statement(self):
        t = self.token
        if self.accept("IF"):
            branches = []
            while True:
                condition = self.expression()
                self.expect("THEN")
                branches.append((condition, self.statements({"ELSIF", "ELSE", "END_IF"})))
                if not self.accept("ELSIF"):
                    break
            otherwise = self.statements({"END_IF"}) if self.accept("ELSE") else []
            self.expect("END_IF")
            result = Node("if", (branches, otherwise), t)
        elif self.accept("FOR"):
            variable = self.expression()
            self.expect(":=")
            first = self.expression()
            self.expect("TO")
            last = self.expression()
            step = self.expression() if self.accept("BY") else Node("literal", ("1",), t)
            self.expect("DO")
            body = self.statements({"END_FOR"})
            self.expect("END_FOR")
            result = Node("for", (variable, first, last, step, body), t)
        elif self.accept("WHILE"):
            condition = self.expression()
            self.expect("DO")
            body = self.statements({"END_WHILE"})
            self.expect("END_WHILE")
            result = Node("while", (condition, body), t)
        elif self.accept("CASE"):
            selector = self.expression()
            self.expect("OF")
            arms = []
            while not self.is_("ELSE") and not self.is_("END_CASE"):
                labels = []
                while True:
                    low = self.expression()
                    high = self.expression() if self.accept("..") else low
                    labels.append((low, high))
                    if not self.accept(","):
                        break
                self.expect(":")
                arms.append((labels, self.statements({"ELSE", "END_CASE"}, case=True)))
            otherwise = self.statements({"END_CASE"}) if self.accept("ELSE") else []
            self.expect("END_CASE")
            result = Node("case", (selector, arms, otherwise), t)
        elif self.accept("RETURN"):
            result = Node("return", (), t)
        elif self.accept("EXIT"):
            result = Node("exit", (), t)
        else:
            expr = self.expression()
            if self.accept(":="):
                if expr.op != "ref":
                    t.fail("assignment requires a variable")
                result = Node("assign", (expr, self.expression()), t)
            elif expr.op == "call":
                result = Node("call", (expr,), t)
            else:
                t.fail("expected assignment or function call")
        self.expect(";")
        return result


def walk(value):
    """Walk expressions/statements, including selectors, arguments and branch bodies."""
    if isinstance(value, Node):
        yield value
        yield from walk(value.args)
    elif isinstance(value, (tuple, list)):
        for item in value:
            yield from walk(item)


def constant(node):
    if node.op == "typed":
        if node.args[0] not in {"BOOL", "BYTE", "WORD", "DWORD", "INT", "DINT", "B", "W", "DW", "L"}:
            node.token.fail(f"unsupported constant type {node.args[0]}")
        return constant(node.args[1])
    if node.op == "literal":
        raw = node.args[0].upper().replace("_", "")
        if raw in {"TRUE", "FALSE"}:
            return int(raw == "TRUE")
        if "#" in raw:
            base, digits = raw.split("#")
            return int(digits, int(base))
        if re.fullmatch(r"\d+", raw):
            return int(raw)
    if node.op == "unary" and node.args[0] in {"+", "-"}:
        value = constant(node.args[1])
        return value if node.args[0] == "+" else -value
    node.token.fail("expected integer/boolean constant")
