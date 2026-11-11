"""Language-agnostic messaging rules for ``ncp ontology scan``.

A declarative rule table (:data:`RULES`) finds produce / consume call sites for
Kafka, SQS, SNS, RabbitMQ / AMQP and JMS clients in JS/TS, Python, Go and
JVM sources. A rule is a compiled regex that ends at the *destination expression*
(group ``args``) plus a ``mode`` that says how to cut that expression out of the
argument list. The expression is resolved by the shared resolver in
:mod:`ncp.ontology.scan` (literals, constants, ``${placeholders}``, SpEL, env
defaults); this module only finds sites and builds the generic constant index.

Everything is linear in the input. Regexes use bounded repetition (``{0,N}``) and
no nested unbounded quantifiers; argument lists are cut with a string- and
bracket-aware scanner limited to :data:`WINDOW` characters; lines longer than
:data:`MAX_LINE` (minified / generated code) are blanked before matching.
"""

from __future__ import annotations

import re
from bisect import bisect_right
from collections.abc import Callable, Collection
from dataclasses import dataclass, field

MAX_LINE = 2000  # longer lines are minified / generated; never worth parsing
WINDOW = 400  # characters of argument text a rule looks at
_MAX_OBJECTS = 200  # object / enum / class constant blocks parsed per file
_MAX_SITES = 500  # matches of one rule parsed per file (generated files hold thousands)

JS = (".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".mts", ".cts")
PY = (".py",)
GO = (".go",)
JVM = (".java", ".kt", ".scala")

# Cheap prefilter: a file is only parsed when it mentions one of these (any case).
KEYWORDS = (
    "kafka", "sqs", "sns", "amqp", "rabbit", "jms", "queue", "exchange", "publish",
    "sendtoqueue", "queueurl", "topicarn", "destination", "streambridge", "sendto",
    # Generic call words: the client is often created in another file of the module, so the
    # file itself never names the broker (the module context decides in :func:`find_sites`).
    "producer", "consume", "subscribe", "basic_publish",
)


def is_unresolved_key(key: str) -> bool:
    """True for a Queue / Exchange / KafkaTopic key that holds a placeholder
    (``<broker>:unresolved:<x>`` or ``unresolved:<x>``)."""
    return _UNRESOLVED_KEY.search(key) is not None


def is_pattern_key(key: str) -> bool:
    """True for a pattern key (``<broker>:pattern:<x>`` or ``pattern:<x>``)."""
    return _PATTERN_KEY.search(key) is not None


_UNRESOLVED_KEY = re.compile(r"(?:^|:)unresolved:")
_PATTERN_KEY = re.compile(r"(?:^|:)pattern:")


def mentions_messaging(text: str) -> str | None:
    """The lower-cased ``text`` when it mentions any broker keyword, else None."""
    lowered = text.lower()
    return lowered if any(word in lowered for word in KEYWORDS) else None


# -- text helpers (shared with scan.py) ------------------------------------------------


def collapse(text: str, limit: int = 200) -> str:
    return " ".join(text.split())[:limit]


_ESCAPE = re.compile(r"\\(.)", re.DOTALL)  # one fixed-width token per match: linear


def unescape(value: str) -> str:
    """Resolve ``\\$``, ``\\"`` and ``\\\\``; other escapes are left as written."""
    if "\\" not in value:
        return value
    return _ESCAPE.sub(lambda m: m.group(1) if m.group(1) in '\\"$' else m.group(0), value)


def matching(text: str, open_index: int, limit: int = 8000) -> int:
    """Index of the bracket closing ``text[open_index]`` (string aware), or -1."""
    depth = 0
    quote = ""
    index = open_index
    stop = min(len(text), open_index + limit)
    while index < stop:
        char = text[index]
        if quote:
            if char == "\\":
                index += 1
            elif char == quote:
                quote = ""
        elif char in "\"'":
            quote = char
        elif char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
            if depth == 0:
                return index
        index += 1
    return -1


def split_top(text: str, sep: str, base: int = 0) -> list[tuple[int, str]]:
    """Split on ``sep`` outside strings and brackets; offsets are ``base``-relative."""
    pieces: list[tuple[int, str]] = []
    depth = 0
    quote = ""
    start = 0
    index = 0
    while index < len(text):
        char = text[index]
        if quote:
            if char == "\\":
                index += 1
            elif char == quote:
                quote = ""
        elif char in "\"'":
            quote = char
        elif char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
        elif char == sep and depth == 0:
            pieces.append((base + start, text[start:index]))
            start = index + 1
        index += 1
    pieces.append((base + start, text[start:]))
    return pieces


def call_args(tail: str) -> tuple[list[tuple[int, str]], int]:
    """Top-level, comma separated arguments at the start of ``tail`` and the index where
    the enclosing call / object / list closes (``len(tail)`` when it does not).

    String aware (``"``, ``'``, backtick; a ``"``/``'`` string ends at a newline);
    one pass, no backtracking."""
    items: list[tuple[int, str]] = []
    depth = 0
    quote = ""
    start = 0
    index = 0
    size = len(tail)
    while index < size:
        char = tail[index]
        if quote:
            if char == "\\":
                index += 1
            elif char == quote or (char == "\n" and quote != "`"):
                quote = ""
        elif char in "\"'`":
            quote = char
        elif char in "([{":
            depth += 1
        elif char in ")]}":
            if depth == 0:
                break
            depth -= 1
        elif depth == 0 and char == ",":
            items.append((start, tail[start:index]))
            start = index + 1
        elif depth == 0 and char == ";":
            break
        index += 1
    items.append((start, tail[start:index]))
    return [(off, text) for off, text in items if text.strip()], index


def unquote(expr: str) -> str | None:
    """The content of a plain ``"..."`` / ``'...'`` / `` `...` `` literal, else None."""
    text = expr.strip()
    if len(text) < 2 or text[0] not in "\"'`" or text[-1] != text[0]:
        return None
    quote, inner = text[0], text[1:-1]
    if quote in inner.replace("\\\\", "").replace("\\" + quote, ""):
        return None
    if quote == "`" and "${" in inner:
        return None
    return unescape(inner)


_JS_ENV = re.compile(
    r"process\.env(?:\.([A-Za-z_]\w*)|\[\s*[\"']([^\"']{1,100})[\"']\s*\])!?"
    r"(?:\s*(?:\?\?|\|\|)\s*(.{1,200}))?",
)
_PY_ENV_GET = re.compile(
    r"os\s*\.\s*(?:getenv|environ\s*\.\s*get)\s*\(\s*[\"']([^\"']{1,100})[\"']\s*"
    r"(?:,\s*([^()]{1,200}?)\s*)?\)(?:\s*or\s+(.{1,200}))?",
)
_PY_ENV_ITEM = re.compile(r"os\s*\.\s*environ\s*\[\s*[\"']([^\"']{1,100})[\"']\s*\]")
_GO_ENV = re.compile(r"os\s*\.\s*Getenv\s*\(\s*\"([^\"]{1,100})\"\s*\)")


def parse_env(expr: str) -> tuple[str, str | None] | None:
    """``(variable, default)`` for an environment lookup expression, else None:
    ``process.env.X ?? "d"``, ``os.getenv("X", "d")``, ``os.environ.get``, ``os.Getenv``.
    The default is None when there is none or it is not a plain literal."""
    text = expr.strip()
    if len(text) > 400:
        return None
    match = _JS_ENV.fullmatch(text)
    if match:
        return match.group(1) or match.group(2), unquote(match.group(3)) if match.group(3) else None
    match = _PY_ENV_GET.fullmatch(text)
    if match:
        default = match.group(2) or match.group(3)
        return match.group(1), unquote(default) if default else None
    match = _PY_ENV_ITEM.fullmatch(text) or _GO_ENV.fullmatch(text)
    if match:
        return match.group(1), None
    return None


def unwrap(expr: str) -> str:
    """Strip TypeScript ``!`` / ``as string`` and Go ``aws.String(...)`` wrappers."""
    text = expr.strip()
    for tail in (" as string", " as const"):
        if text.endswith(tail):
            text = text[:-len(tail)].rstrip()
    for head in ("aws.String(", "aws.ToString("):
        if text.startswith(head) and text.endswith(")"):
            return unwrap(text[len(head):-1])
    if text.endswith("!") and not text.endswith("!="):
        text = text[:-1].rstrip()
    return text


_DEST_URL = re.compile(r"https?://[^/\s]{1,200}/[^\s?#]{1,300}")


def dest_name(value: str) -> tuple[str, dict[str, object]]:
    """Queue / topic name from a URL (last path segment) or an ARN (last ``:`` segment);
    the original value is kept in ``url`` / ``arn``."""
    text = value.strip()
    if text.startswith("arn:") and text.count(":") >= 5:
        return text.rsplit(":", 1)[-1], {"arn": text}
    if _DEST_URL.fullmatch(text.split("?", 1)[0].split("#", 1)[0]):
        path = text.split("://", 1)[1].split("?", 1)[0].split("#", 1)[0].rstrip("/")
        return path.rsplit("/", 1)[-1], {"url": text}
    return text, {}


# -- rules ----------------------------------------------------------------------------


@dataclass(frozen=True)
class Rule:
    id: str
    broker: str  # kafka | sqs | sns | rabbitmq (every AMQP 0-9-1 client) | jms
    entity: str  # KafkaTopic | Queue | Exchange
    direction: str  # PRODUCES | CONSUMES | ROUTES_TO
    suffixes: tuple[str, ...]
    pattern: re.Pattern[str]  # ends at the destination expression
    mode: str = "first"  # how the group is cut into destination expressions
    group: str = "args"  # regex group holding the destination expression(s)
    requires: tuple[str, ...] = ()  # the owning module must mention one of these (lower-case)
    skip: int = 0  # leading positional arguments to ignore (e.g. a Go ``ctx``)
    keys: tuple[str, ...] = ()  # keyword / annotation attribute names


@dataclass
class Dest:
    entity: str
    expr: str
    line: int


@dataclass
class Site:
    rule: Rule
    line: int
    direction: str
    dests: list[Dest] = field(default_factory=list)  # empty: not understood

    @property
    def broker(self) -> str:
        return self.rule.broker


# A lookahead: the argument text is read but not consumed, so neighbouring calls still match.
_ARGS = r"(?=(?P<args>[\s\S]{0,%d}))" % WINDOW
# Up to one nested level of braces / parens between a call and a named property; both
# alternatives start with different characters, so the bounded repetition cannot blow up.
_NEST_B = r"(?:[^{}]|\{[^{}]{0,120}\}){0,300}?"
_NEST_P = r"(?:[^()]|\([^()]{0,120}\)){0,300}?"
_PRODUCER = r"[Pp]roducer\w{0,30}\s*\.\s*"
_CONSUMER = r"[Cc]onsumer\w{0,30}\s*\.\s*"
_CHANNEL = r"(?:\b[Cc]h|[Cc]hannel)\w{0,20}\s*\.\s*"
_OBJ = r"\s*\(\s*\{"  # ``( {``


def _rule(
    rule_id: str, broker: str, entity: str, direction: str, suffixes: tuple[str, ...],
    pattern: str, mode: str = "first", **options: object,
) -> Rule:
    return Rule(rule_id, broker, entity, direction, suffixes, re.compile(pattern), mode,
                **options)  # type: ignore[arg-type]


_RULES: list[Rule] = [
    # -- JS / TS: kafkajs, node-rdkafka ------------------------------------------------
    _rule("kafkajs.producer.send", "kafka", "KafkaTopic", "PRODUCES", JS,
          _PRODUCER + "send" + _OBJ + _NEST_B + r"\btopic\s*:\s*" + _ARGS,
          requires=("kafka",)),
    _rule("kafkajs.producer.sendBatch", "kafka", "KafkaTopic", "PRODUCES", JS,
          _PRODUCER + "sendBatch" + _OBJ + _NEST_B + r"\btopicMessages\s*:\s*"
          + r"(?=(?P<args>[\s\S]{0,1500}))", "batch", requires=("kafka",)),
    _rule("kafkajs.consumer.subscribe", "kafka", "KafkaTopic", "CONSUMES", JS,
          _CONSUMER + "subscribe" + _OBJ + _NEST_B + r"\btopics?\s*:\s*" + _ARGS, "list",
          requires=("kafka",)),
    _rule("rdkafka.producer.produce", "kafka", "KafkaTopic", "PRODUCES", JS,
          _PRODUCER + r"produce\s*\(\s*" + _ARGS, requires=("kafka",)),
    # -- JS / TS: amqplib --------------------------------------------------------------
    _rule("amqplib.sendToQueue", "rabbitmq", "Queue", "PRODUCES", JS,
          r"\.\s*sendToQueue\s*\(\s*" + _ARGS, requires=("amqp", "rabbit")),
    _rule("amqplib.publish", "rabbitmq", "Exchange", "PRODUCES", JS,
          _CHANNEL + r"publish\s*\(\s*" + _ARGS, "publish", requires=("amqp", "rabbit")),
    _rule("amqplib.consume", "rabbitmq", "Queue", "CONSUMES", JS,
          _CHANNEL + r"consume\s*\(\s*" + _ARGS, requires=("amqp", "rabbit")),
    _rule("amqplib.bindQueue", "rabbitmq", "Exchange", "ROUTES_TO", JS,
          r"\.\s*bindQueue\s*\(\s*" + _ARGS, "bind", requires=("amqp", "rabbit")),
    # -- JS / TS: AWS SDK v3, sqs-consumer ---------------------------------------------
    _rule("aws3.SendMessageCommand", "sqs", "Queue", "PRODUCES", JS,
          r"new\s+SendMessage(?:Batch)?Command" + _OBJ + _NEST_B + r"\bQueueUrl\s*:\s*" + _ARGS),
    _rule("aws3.ReceiveMessageCommand", "sqs", "Queue", "CONSUMES", JS,
          r"new\s+ReceiveMessageCommand" + _OBJ + _NEST_B + r"\bQueueUrl\s*:\s*" + _ARGS),
    _rule("sqs-consumer.create", "sqs", "Queue", "CONSUMES", JS,
          r"Consumer\s*\.\s*create" + _OBJ + _NEST_B + r"\bqueueUrl\s*:\s*" + _ARGS,
          requires=("sqs",)),
    _rule("aws3.PublishCommand", "sns", "Exchange", "PRODUCES", JS,
          r"new\s+Publish(?:Batch)?Command" + _OBJ + _NEST_B + r"\bTopicArn\s*:\s*" + _ARGS),
    # -- Python: confluent-kafka / kafka-python / aiokafka -----------------------------
    _rule("py.kafka.produce", "kafka", "KafkaTopic", "PRODUCES", PY,
          _PRODUCER + r"(?:produce|send|send_and_wait)\s*\(\s*" + _ARGS,
          keys=("topic",), requires=("kafka",)),
    _rule("py.kafka.subscribe", "kafka", "KafkaTopic", "CONSUMES", PY,
          r"\.\s*subscribe\s*\(\s*(?:topics\s*=\s*)?(?=(?P<args>\[[\s\S]{0,%d}))" % WINDOW, "list",
          requires=("kafka",)),
    _rule("py.kafka.consumer", "kafka", "KafkaTopic", "CONSUMES", PY,
          r"(?:KafkaConsumer|AIOKafkaConsumer)\s*\(\s*" + _ARGS, "leading",
          requires=("kafka",)),
    # -- Python: boto3, pika -----------------------------------------------------------
    _rule("boto3.send_message", "sqs", "Queue", "PRODUCES", PY,
          r"\bsend_message(?:_batch)?\s*\(\s*" + _NEST_P + r"\bQueueUrl\s*=\s*" + _ARGS),
    _rule("boto3.receive_message", "sqs", "Queue", "CONSUMES", PY,
          r"\breceive_message\s*\(\s*" + _NEST_P + r"\bQueueUrl\s*=\s*" + _ARGS),
    _rule("boto3.publish", "sns", "Exchange", "PRODUCES", PY,
          r"\.\s*publish(?:_batch)?\s*\(\s*" + _NEST_P + r"\bTopicArn\s*=\s*" + _ARGS),
    _rule("pika.basic_publish", "rabbitmq", "Exchange", "PRODUCES", PY,
          r"\.\s*basic_publish\s*\(\s*" + _ARGS, "publish", keys=("exchange", "routing_key")),
    _rule("pika.basic_consume", "rabbitmq", "Queue", "CONSUMES", PY,
          r"\.\s*basic_consume\s*\(\s*" + _ARGS, keys=("queue",)),
    _rule("pika.queue_bind", "rabbitmq", "Exchange", "ROUTES_TO", PY,
          r"\.\s*queue_bind\s*\(\s*" + _ARGS, "bind", keys=("queue", "exchange")),
    # -- Go: kafka-go, sarama, aws-sdk-go, amqp091 -------------------------------------
    _rule("kafka-go.Writer", "kafka", "KafkaTopic", "PRODUCES", GO,
          r"kafka\s*\.\s*Writer\s*\{" + _NEST_B + r"\bTopic\s*:\s*" + _ARGS,
          requires=("kafka",)),
    _rule("kafka-go.ReaderConfig", "kafka", "KafkaTopic", "CONSUMES", GO,
          r"kafka\s*\.\s*ReaderConfig\s*\{" + _NEST_B + r"\bTopic\s*:\s*" + _ARGS,
          requires=("kafka",)),
    _rule("sarama.ProducerMessage", "kafka", "KafkaTopic", "PRODUCES", GO,
          r"ProducerMessage\s*\{" + _NEST_B + r"\bTopic\s*:\s*" + _ARGS,
          requires=("kafka", "sarama")),
    _rule("sarama.ConsumePartition", "kafka", "KafkaTopic", "CONSUMES", GO,
          r"\.\s*ConsumePartition\s*\(\s*" + _ARGS, requires=("kafka", "sarama")),
    _rule("awsgo.SendMessageInput", "sqs", "Queue", "PRODUCES", GO,
          r"SendMessage(?:Batch)?Input\s*\{" + _NEST_B + r"\bQueueUrl\s*:\s*" + _ARGS),
    _rule("awsgo.ReceiveMessageInput", "sqs", "Queue", "CONSUMES", GO,
          r"ReceiveMessageInput\s*\{" + _NEST_B + r"\bQueueUrl\s*:\s*" + _ARGS),
    _rule("awsgo.PublishInput", "sns", "Exchange", "PRODUCES", GO,
          r"PublishInput\s*\{" + _NEST_B + r"\bTopicArn\s*:\s*" + _ARGS),
    _rule("amqp091.Publish", "rabbitmq", "Exchange", "PRODUCES", GO,
          _CHANNEL + r"Publish\s*\(\s*" + _ARGS, "publish", requires=("amqp",)),
    _rule("amqp091.PublishWithContext", "rabbitmq", "Exchange", "PRODUCES", GO,
          _CHANNEL + r"PublishWithContext\s*\(\s*" + _ARGS, "publish", requires=("amqp",),
          skip=1),
    _rule("amqp091.Consume", "rabbitmq", "Queue", "CONSUMES", GO,
          _CHANNEL + r"Consume\s*\(\s*" + _ARGS, requires=("amqp",)),
    # -- JVM: Spring AMQP, JMS, Spring Cloud AWS, AWS SDK v2 ---------------------------
    _rule("spring-amqp.send", "rabbitmq", "Exchange", "PRODUCES", JVM,
          r"(?:[Rr]abbit|[Aa]mqp)\w{0,30}\s*\.\s*(?:convertAndSend|convertSendAndReceive|send)"
          r"\s*\(\s*" + _ARGS, "spring_amqp"),
    _rule("spring-amqp.RabbitListener", "rabbitmq", "Queue", "CONSUMES", JVM,
          r"@RabbitListener\s*\(\s*" + _ARGS, "annotation",
          keys=("queues", "queuesToDeclare")),
    _rule("jms.send", "jms", "Queue", "PRODUCES", JVM,
          r"[Jj]ms\w{0,30}\s*\.\s*(?:convertAndSend|send)\s*\(\s*" + _ARGS),
    _rule("jms.JmsListener", "jms", "Queue", "CONSUMES", JVM,
          r"@JmsListener\s*\(\s*" + _ARGS, "annotation", keys=("destination",)),
    _rule("spring-aws.SqsListener", "sqs", "Queue", "CONSUMES", JVM,
          r"@SqsListener\s*\(\s*" + _ARGS, "annotation", keys=("", "value", "queueNames")),
    _rule("spring-aws.send", "sqs", "Queue", "PRODUCES", JVM,
          r"[Ss]qs\w{0,30}\s*\.\s*send(?:Async)?\s*\(\s*+(?!\w+\s*->)" + _ARGS),
    _rule("spring-aws.send.lambda", "sqs", "Queue", "PRODUCES", JVM,
          r"[Ss]qs\w{0,30}\s*\.\s*send\w{0,10}\s*\(\s*\w+\s*->\s*\w+\s*\.\s*queue\s*\(\s*"
          + _ARGS),
    _rule("aws2.SendMessageRequest", "sqs", "Queue", "PRODUCES", JVM,
          r"SendMessage(?:Batch)?Request\s*\.\s*builder\s*\(\s*\)[^;]{0,400}?"
          r"\.\s*queueUrl\s*\(\s*" + _ARGS),
    _rule("aws2.ReceiveMessageRequest", "sqs", "Queue", "CONSUMES", JVM,
          r"ReceiveMessageRequest\s*\.\s*builder\s*\(\s*\)[^;]{0,400}?"
          r"\.\s*queueUrl\s*\(\s*" + _ARGS),
    _rule("aws2.PublishRequest", "sns", "Exchange", "PRODUCES", JVM,
          r"PublishRequest\s*\.\s*builder\s*\(\s*\)[^;]{0,400}?\.\s*topicArn\s*\(\s*" + _ARGS),
]
RULES: tuple[Rule, ...] = tuple(_RULES)
REQUIRE_WORDS: tuple[str, ...] = tuple(sorted({word for rule in RULES for word in rule.requires}))


def context_words(lowered: str) -> set[str]:
    """The ``requires`` words (:data:`REQUIRE_WORDS`) that occur in lower-cased ``lowered``;
    the unit a module's messaging context is built from (files, build manifests)."""
    return {word for word in REQUIRE_WORDS if word in lowered}


# -- turning a matched argument tail into destination expressions -------------------------

_KW = re.compile(r"\s*([A-Za-z_]\w*)\s*=(?!=)\s*([\s\S]*)")
_LIST_CALL = re.compile(r"(?:arrayOf|listOf|arrayListOf|setOf)\s*\(([\s\S]*)\)")

_Items = list[tuple[int, str]]
_LineAt = Callable[[int], int]


def _pick(items: _Items, pos: int, keys: tuple[str, ...]) -> tuple[int, str] | None:
    """The argument named by one of ``keys`` (keyword), else positional argument ``pos``."""
    positional: _Items = []
    for offset, text in items:
        match = _KW.fullmatch(text) if keys else None
        if match:
            if match.group(1) in keys:
                return offset + match.start(2), match.group(2)
            continue
        positional.append((offset, text))
    return positional[pos] if pos < len(positional) else None


def _dest(entity: str, picked: tuple[int, str], base: int, line_at: _LineAt) -> Dest:
    offset, text = picked
    lead = len(text) - len(text.lstrip())
    return Dest(entity, text.strip(), line_at(base + offset + lead))


def _expand_list(value: str) -> _Items:
    """``[A, B]`` / ``{A, B}`` / ``arrayOf(A, B)`` -> items (offsets relative to ``value``)."""
    text = value.strip()
    lead = value.index(text[0]) if text else 0
    inner = None
    if text[:1] in ("[", "{") and text[-1:] in ("]", "}"):
        inner, shift = text[1:-1], lead + 1
    else:
        match = _LIST_CALL.fullmatch(text)
        if match:
            inner, shift = match.group(1), lead + match.start(1)
    if inner is None:
        return [(lead, text)] if text else []
    return [(shift + off, part) for off, part in split_top(inner, ",") if part.strip()]


def _mode_first(rule: Rule, args: str, base: int, line_at: _LineAt) -> list[Dest]:
    items, _end = call_args(args)
    picked = _pick(items, 0, rule.keys)
    return [_dest(rule.entity, picked, base, line_at)] if picked else []


def _mode_list(rule: Rule, args: str, base: int, line_at: _LineAt) -> list[Dest]:
    items, _end = call_args(args)
    picked = _pick(items, 0, rule.keys)
    if picked is None:
        return []
    offset, text = picked
    return [_dest(rule.entity, (offset + off, part), base, line_at)
            for off, part in _expand_list(text)]


def _mode_leading(rule: Rule, args: str, base: int, line_at: _LineAt) -> list[Dest]:
    out: list[Dest] = []
    for offset, text in call_args(args)[0]:
        if _KW.fullmatch(text):
            break
        out.append(_dest(rule.entity, (offset, text), base, line_at))
    return out


_TOPIC_KEY = re.compile(r"\btopic\s*:\s*")


def _mode_batch(rule: Rule, args: str, base: int, line_at: _LineAt) -> list[Dest]:
    end = call_args(args)[1]
    body = args[:end]
    out: list[Dest] = []
    for found in _TOPIC_KEY.finditer(body):
        items, _stop = call_args(body[found.end():])
        if items:
            out.append(_dest(rule.entity, (found.end() + items[0][0], items[0][1]), base, line_at))
    return out


def _mode_publish(rule: Rule, args: str, base: int, line_at: _LineAt) -> list[Dest]:
    """``publish(exchange, routing_key, ...)``: an empty exchange is the default exchange, so
    the routing key names a queue."""
    items = call_args(args)[0]
    exchange = _pick(items, rule.skip, rule.keys[:1])
    if exchange is None:
        return []
    if unquote(exchange[1]) == "":
        key = _pick(items, rule.skip + 1, rule.keys[1:2])
        return [_dest("Queue", key, base, line_at)] if key else []
    return [_dest("Exchange", exchange, base, line_at)]


def _mode_bind(rule: Rule, args: str, base: int, line_at: _LineAt) -> list[Dest]:
    """``bindQueue(queue, exchange, key)``: ``[exchange, queue]`` (the ROUTES_TO direction)."""
    items = call_args(args)[0]
    queue = _pick(items, 0, rule.keys[:1])
    exchange = _pick(items, 1, rule.keys[1:2])
    if queue is None or exchange is None:
        return []
    return [_dest("Exchange", exchange, base, line_at), _dest("Queue", queue, base, line_at)]


def _mode_spring_amqp(rule: Rule, args: str, base: int, line_at: _LineAt) -> list[Dest]:
    """``(exchange, routingKey, message)`` names an exchange, ``(routingKey, message)`` a
    queue through the default exchange; a lone message names nothing."""
    items = call_args(args)[0]
    if len(items) >= 3:
        return [_dest("Exchange", items[0], base, line_at)]
    if len(items) == 2:
        return [_dest("Queue", items[0], base, line_at)]
    return []


def _annotation_value(expr: str) -> tuple[int, str] | None:
    """``@Queue("x")`` / ``@Queue(value = "x")`` -> the name expression."""
    text = expr.strip()
    if not text.startswith("@"):
        return 0, text
    open_index = text.find("(")
    if open_index < 0:
        return None
    items = call_args(text[open_index + 1:])[0]
    picked = _pick(items, 0, ("value", "name"))
    return (open_index + 1 + picked[0], picked[1]) if picked else None


def _mode_annotation(rule: Rule, args: str, base: int, line_at: _LineAt) -> list[Dest]:
    out: list[Dest] = []
    for offset, text in call_args(args)[0]:
        match = _KW.fullmatch(text)
        key, value = (match.group(1), match.group(2)) if match else ("", text)
        if key not in rule.keys:
            continue
        line = line_at(base + offset)
        for _off, item in _expand_list(value):
            named = _annotation_value(item)
            if named is not None and named[1].strip():
                out.append(Dest(rule.entity, named[1].strip(), line))
    return out


_MODES: dict[str, Callable[[Rule, str, int, _LineAt], list[Dest]]] = {
    "first": _mode_first, "list": _mode_list, "leading": _mode_leading, "batch": _mode_batch,
    "publish": _mode_publish, "bind": _mode_bind, "spring_amqp": _mode_spring_amqp,
    "annotation": _mode_annotation,
}


def line_starts(text: str) -> list[int]:
    starts = [0]
    index = text.find("\n")
    while index >= 0:
        starts.append(index + 1)
        index = text.find("\n", index + 1)
    return starts


def blank_long_lines(text: str) -> str:
    """Blank (keep the newline of) lines longer than :data:`MAX_LINE`."""
    if len(text) <= MAX_LINE:
        return text
    lines = text.split("\n")
    if all(len(line) <= MAX_LINE for line in lines):
        return text
    return "\n".join(line if len(line) <= MAX_LINE else "" for line in lines)


_COMMENT = ("//", "/*", "*", "#")


def find_sites(
    suffix: str, text: str, lowered: str | None = None, context: Collection[str] | str = (),
) -> list[Site]:
    """Every rule match in ``text`` (a file with extension ``suffix``), sorted by line.

    A rule with ``requires`` applies when the file *or* its module context mentions one of the
    words. ``context`` is the owning module's lower-case words (see :func:`context_words`:
    other source files of the module and its build manifest dependencies), because the client
    is usually created in a different file than the one that calls ``producer.send``."""
    lowered = lowered if lowered is not None else text.lower()
    rules = [rule for rule in RULES if suffix in rule.suffixes
             and (not rule.requires
                  or any(word in lowered or word in context for word in rule.requires))]
    if not rules:
        return []
    text = blank_long_lines(text)
    starts = line_starts(text)

    def line_at(offset: int) -> int:
        return bisect_right(starts, offset)

    sites: list[Site] = []
    for rule in rules:
        mode = _MODES[rule.mode]
        parsed = 0
        for match in rule.pattern.finditer(text):
            if parsed >= _MAX_SITES:
                break
            parsed += 1
            line = line_at(match.start())
            if text[starts[line - 1]:match.start()].lstrip().startswith(_COMMENT):
                continue
            args = match.group(rule.group)
            sites.append(Site(rule, line, rule.direction,
                              mode(rule, args, match.start(rule.group), line_at)))
    sites.sort(key=lambda site: site.line)
    return sites


# -- generic constant index (non-JVM) -----------------------------------------------------

Constant = tuple[str | None, str, str, str | None]  # (owner, NAME, value, env variable)


def _literal_value(rest: str) -> tuple[str, str | None] | None:
    """``(value, env)`` for the right-hand side of a constant: a plain string literal, or an
    environment lookup with a default (``env`` names the variable)."""
    text = rest.strip()
    if not text:
        return None
    if text[0] in "\"'`":
        end = 1
        while end < len(text) and text[end] != text[0]:
            end += 2 if text[end] == "\\" else 1
        if end >= len(text):
            return None
        value = unquote(text[:end + 1])
        trailing = text[end + 1:].strip()
        if value is None or (trailing and not trailing.startswith(
                (";", ",", "//", "#", "/*", "as ", "satisfies", ")"))):
            return None
        return value, None
    env = parse_env(text.rstrip(";, "))
    if env and env[1] is not None:
        return env[1], env[0]
    return None


_JS_DECL = re.compile(
    r"(?:^|[\s;{(])(?:export\s+)?(?:declare\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*"
    r"(?::[^=\n]{1,80})?=(?!=)\s*",
)
_JS_ENUM = re.compile(r"\benum\s+([A-Za-z_$][\w$]*)\s*\{")


def _object_entries(
    text: str, open_index: int, owner: str, separator: str,
) -> list[Constant]:
    end = matching(text, open_index, 2000)
    if end < 0:
        return []
    out: list[Constant] = []
    for _off, piece in split_top(text[open_index + 1:end], ","):
        key, sep, value = piece.partition(separator)
        key = key.strip().strip("\"'")
        if not sep or not re.fullmatch(r"[A-Za-z_$][\w$]*", key):
            continue
        parsed = _literal_value(value)
        if parsed:
            out.append((owner, key, parsed[0], parsed[1]))
    return out


def _js_constants(text: str) -> list[Constant]:
    found: list[Constant] = []
    objects = 0
    offset = 0
    for raw in text.split("\n"):
        line_offset, offset = offset, offset + len(raw) + 1
        if len(raw) > MAX_LINE or raw.lstrip().startswith(("//", "*", "/*")):
            continue
        decl = _JS_DECL.search(raw)
        if decl:
            rest = raw[decl.end():]
            if rest.startswith("{"):
                if objects < _MAX_OBJECTS:
                    objects += 1
                    found.extend(_object_entries(text, line_offset + decl.end(),
                                                 decl.group(1), ":"))
                continue
            parsed = _literal_value(rest)
            if parsed:
                found.append((None, decl.group(1), parsed[0], parsed[1]))
            continue
        enum = _JS_ENUM.search(raw)
        if enum and objects < _MAX_OBJECTS:
            objects += 1
            found.extend(_object_entries(text, line_offset + enum.end() - 1, enum.group(1), "="))
    return found


_PY_CLASS = re.compile(r"class\s+([A-Za-z_]\w*)")
_PY_ASSIGN = re.compile(r"([A-Za-z_]\w*)\s*(?::[^=\n]{1,60})?=(?!=)\s*")
_PY_SELF = re.compile(r"self\s*\.\s*([A-Za-z_]\w*)\s*(?::[^=\n]{1,60})?=(?!=)\s*")


def _py_constants(text: str) -> list[Constant]:
    found: list[Constant] = []
    owner: str | None = None
    body_indent = -1
    for raw in text.split("\n"):
        stripped = raw.strip()
        if not stripped or stripped[0] == "#" or len(raw) > MAX_LINE:
            continue
        indent = len(raw) - len(raw.lstrip(" \t"))
        if indent == 0:
            owner, body_indent = None, -1
            cls = _PY_CLASS.match(raw)
            if cls:
                owner = cls.group(1)
                continue
            assign = _PY_ASSIGN.match(raw)
            if assign:
                parsed = _literal_value(raw[assign.end():])
                if parsed:
                    found.append((None, assign.group(1), parsed[0], parsed[1]))
            continue
        if owner is None:
            continue
        if body_indent < 0:
            body_indent = indent
        self_assign = _PY_SELF.match(stripped)
        if self_assign:
            parsed = _literal_value(stripped[self_assign.end():])
            if parsed:
                found.append((owner, self_assign.group(1), parsed[0], parsed[1]))
            continue
        assign = _PY_ASSIGN.match(stripped) if indent == body_indent else None
        if assign:
            parsed = _literal_value(stripped[assign.end():])
            if parsed:
                found.append((owner, assign.group(1), parsed[0], parsed[1]))
    return found


_GO_BLOCK = re.compile(r"\s*(?:const|var)\s*\(\s*(?://.*)?$")
_GO_SINGLE = re.compile(r"\s*(?:const|var)\s+([A-Za-z_]\w*)\s*(?:[\w.*\[\]]{1,60}\s*)?=\s*")
_GO_ENTRY = re.compile(r"\s*([A-Za-z_]\w*)\s*(?:[\w.*\[\]]{1,60}\s*)?=\s*")


def _go_constants(text: str) -> list[Constant]:
    found: list[Constant] = []
    in_block = False
    for raw in text.split("\n"):
        if len(raw) > MAX_LINE:
            continue
        if in_block:
            if raw.lstrip().startswith(")"):
                in_block = False
                continue
            entry = _GO_ENTRY.match(raw)
            if entry:
                parsed = _literal_value(raw[entry.end():])
                if parsed:
                    found.append((None, entry.group(1), parsed[0], parsed[1]))
            continue
        if _GO_BLOCK.match(raw):
            in_block = True
            continue
        single = _GO_SINGLE.match(raw)
        if single:
            parsed = _literal_value(raw[single.end():])
            if parsed:
                found.append((None, single.group(1), parsed[0], parsed[1]))
    return found


def extract_constants(text: str, suffix: str) -> list[Constant]:
    """``(owner, NAME, value, env)`` string constants of a JS/TS, Python or Go file:
    ``const X = "lit"``, ``export const Topics = { A: "a" }``, ``enum T { A = "a" }``,
    module / class attributes, ``const ( X = "lit" )`` blocks. ``env`` is set when the value
    is the default of an environment lookup."""
    if suffix in JS:
        return _js_constants(text)
    if suffix in PY:
        return _py_constants(text)
    if suffix in GO:
        return _go_constants(text)
    return []
