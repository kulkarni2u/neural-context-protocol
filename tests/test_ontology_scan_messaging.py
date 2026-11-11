"""``ncp ontology scan``: broker-aware messaging rules, SpEL, constants and service attribution."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from click.testing import CliRunner

from ncp.cli import main
from ncp.ontology import scan_messaging as msg
from ncp.ontology.facade import OntologyError
from ncp.ontology.registry import SchemaRegistry
from ncp.ontology.scan import scan_repository

MESSAGING_TYPES = ("PRODUCES", "CONSUMES", "ROUTES_TO")


def _write(root: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


def _edges(manifest, *types: str) -> dict[tuple[str, str, str], object]:
    wanted = types or MESSAGING_TYPES
    return {(r.src, r.type, r.dst): r for r in manifest.relations if r.type in wanted}


def _flows(manifest) -> set[tuple[str, str]]:
    """``(relation type, destination)`` of every messaging edge (ROUTES_TO: ``ex->queue``)."""
    out = set()
    for relation in manifest.relations:
        if relation.type in ("PRODUCES", "CONSUMES"):
            out.add((relation.type, relation.dst))
        elif relation.type == "ROUTES_TO":
            out.add((relation.type, f"{relation.src}->{relation.dst}"))
    return out


def _entity(manifest, entity_type: str, key: str):
    return next(e for e in manifest.entities if e.type == entity_type and e.key == key)


SCAFFOLD = {
    ".ts": {"svc/package.json": '{"name": "svc"}', "svc/Dockerfile": "FROM node\n"},
    ".py": {"svc/pyproject.toml": '[project]\nname = "svc"\n', "svc/Dockerfile": "FROM py\n"},
    ".go": {"svc/go.mod": "module example.com/svc\n", "svc/Dockerfile": "FROM go\n"},
    ".java": {"svc/build.gradle": "plugins { id 'java' }\n", "svc/Dockerfile": "FROM jvm\n"},
}


def _snippet(tmp_path: Path, suffix: str, text: str, **extra: str):
    """Scan one source file inside a service module ``svc`` and return the manifest."""
    kind = ".java" if suffix in (".kt", ".scala") else ".ts" if suffix in msg.JS else suffix
    files = dict(SCAFFOLD[kind])
    files[f"svc/src/main/Main{suffix}" if suffix != ".go" else "svc/main.go"] = text
    files.update({f"svc/{k}": v for k, v in extra.items()})
    return scan_repository(_write(tmp_path / "repo", files)).manifest


# -- B: the rule table, one case per bullet ---------------------------------------------

RULE_CASES = [
    pytest.param(".ts", (
        "import { Kafka } from 'kafkajs'\n"
        "await producer.send({ topic: 'k.orders', messages: [{ value: 'x' }] })\n"
    ), {("PRODUCES", "KafkaTopic:k.orders")}, id="kafkajs-send"),
    pytest.param(".tsx", (
        "import { Kafka } from 'kafkajs'\n"
        "await consumer.subscribe({ topic: 'k.one', fromBeginning: true })\n"
        "await consumer.subscribe({ topics: ['k.two', \"k.three\"] })\n"
    ), {("CONSUMES", "KafkaTopic:k.one"), ("CONSUMES", "KafkaTopic:k.two"),
        ("CONSUMES", "KafkaTopic:k.three")}, id="kafkajs-subscribe"),
    pytest.param(".mts", (
        "import { Kafka } from 'kafkajs'\n"
        "await producer.sendBatch({ topicMessages: [{ topic: 'b.one', messages: [] },\n"
        "  { topic: 'b.two', messages: [{ value: 'v' }] }] })\n"
    ), {("PRODUCES", "KafkaTopic:b.one"), ("PRODUCES", "KafkaTopic:b.two")}, id="kafkajs-batch"),
    pytest.param(".js", (
        "const Kafka = require('node-rdkafka')\n"
        "producer.produce('rd.topic', null, Buffer.from('x'))\n"
    ), {("PRODUCES", "KafkaTopic:rd.topic")}, id="node-rdkafka"),
    pytest.param(".ts", (
        "import amqp from 'amqplib'\n"
        "ch.sendToQueue('q.send', Buffer.from('x'))\n"
        "ch.publish('ex.pub', 'key', Buffer.from('x'))\n"
        "ch.consume('q.recv', (m) => m)\n"
        "await channel.bindQueue('q.bound', 'ex.bound', 'rk')\n"
    ), {("PRODUCES", "Queue:rabbitmq:q.send"), ("PRODUCES", "Exchange:rabbitmq:ex.pub"),
        ("CONSUMES", "Queue:rabbitmq:q.recv"),
        ("ROUTES_TO", "Exchange:rabbitmq:ex.bound->Queue:rabbitmq:q.bound")}, id="amqplib"),
    pytest.param(".ts", (
        "import { SQSClient, SendMessageCommand, ReceiveMessageCommand } from '@aws-sdk/client-sqs'\n"
        "import { Consumer } from 'sqs-consumer'\n"
        "await c.send(new SendMessageCommand({ QueueUrl: 'https://sqs.us-east-1.amazonaws.com/1/out-q', "
        "MessageBody: JSON.stringify({ a: 1 }) }))\n"
        "await c.send(new SendMessageBatchCommand({ QueueUrl: 'batch-q', Entries: [] }))\n"
        "await c.send(new ReceiveMessageCommand({ QueueUrl: 'in-q' }))\n"
        "Consumer.create({ queueUrl: 'sqs-consumer-q', handleMessage })\n"
        "await sns.send(new PublishCommand({ TopicArn: 'arn:aws:sns:us-east-1:1:events', "
        "Message: 'm' }))\n"
    ), {("PRODUCES", "Queue:sqs:out-q"), ("PRODUCES", "Queue:sqs:batch-q"),
        ("CONSUMES", "Queue:sqs:in-q"), ("CONSUMES", "Queue:sqs:sqs-consumer-q"),
        ("PRODUCES", "Exchange:sns:events")}, id="aws-sdk-v3"),
    pytest.param(".py", (
        "from kafka import KafkaProducer, KafkaConsumer\n"
        "self._producer.produce('py.one', value=b'x')\n"
        "producer.send('py.two', b'x')\n"
        "consumer.subscribe(['py.three', 'py.four'])\n"
        "c = KafkaConsumer('py.five', 'py.six', bootstrap_servers='x')\n"
    ), {("PRODUCES", "KafkaTopic:py.one"), ("PRODUCES", "KafkaTopic:py.two"),
        ("CONSUMES", "KafkaTopic:py.three"), ("CONSUMES", "KafkaTopic:py.four"),
        ("CONSUMES", "KafkaTopic:py.five"), ("CONSUMES", "KafkaTopic:py.six")}, id="python-kafka"),
    pytest.param(".py", (
        "import boto3\n"
        "sqs.send_message(QueueUrl='https://sqs.eu-west-1.amazonaws.com/1/py-out', MessageBody=json.dumps({}))\n"
        "sqs.send_message_batch(QueueUrl='py-batch', Entries=[])\n"
        "sqs.receive_message(QueueUrl='py-in', MaxNumberOfMessages=1)\n"
        "sns.publish(TopicArn='arn:aws:sns:eu-west-1:1:py-topic', Message='m')\n"
    ), {("PRODUCES", "Queue:sqs:py-out"), ("PRODUCES", "Queue:sqs:py-batch"),
        ("CONSUMES", "Queue:sqs:py-in"), ("PRODUCES", "Exchange:sns:py-topic")}, id="boto3"),
    pytest.param(".py", (
        "import pika\n"
        "ch.basic_publish(exchange='py.ex', routing_key='k', body=b'x')\n"
        "ch.basic_publish(exchange='', routing_key='py.default', body=b'x')\n"
        "ch.basic_publish('', 'py.positional', b'x')\n"
        "ch.basic_consume(queue='py.consume', on_message_callback=cb)\n"
    ), {("PRODUCES", "Exchange:rabbitmq:py.ex"), ("PRODUCES", "Queue:rabbitmq:py.default"),
        ("PRODUCES", "Queue:rabbitmq:py.positional"), ("CONSUMES", "Queue:rabbitmq:py.consume")},
        id="pika"),
    pytest.param(".go", (
        "package main\nimport kafka \"github.com/segmentio/kafka-go\"\n"
        "var w = &kafka.Writer{Addr: kafka.TCP(\"b\"), Topic: \"go.out\", Brokers: []string{\"x\"}}\n"
        "var r = kafka.NewReader(kafka.ReaderConfig{Brokers: []string{\"b\"}, Topic: \"go.in\"})\n"
        "msg := &sarama.ProducerMessage{Topic: \"sarama.out\", Value: v}\n"
        "pc, _ := consumer.ConsumePartition(\"sarama.in\", 0, sarama.OffsetNewest)\n"
    ), {("PRODUCES", "KafkaTopic:go.out"), ("CONSUMES", "KafkaTopic:go.in"),
        ("PRODUCES", "KafkaTopic:sarama.out"), ("CONSUMES", "KafkaTopic:sarama.in")}, id="go-kafka"),
    pytest.param(".go", (
        "package main\n"
        "in := &sqs.SendMessageInput{QueueUrl: aws.String(\"go-sqs-out\"), MessageBody: aws.String(\"x\")}\n"
        "rc := &sqs.ReceiveMessageInput{QueueUrl: aws.String(\"https://sqs.us-east-1.amazonaws.com/1/go-sqs-in\")}\n"
        "ch.Publish(\"go.ex\", \"key\", false, false, amqp.Publishing{})\n"
        "ch.PublishWithContext(ctx, \"go.ex2\", \"key\", false, false, amqp.Publishing{})\n"
        "ch.Publish(\"\", \"go.default\", false, false, amqp.Publishing{})\n"
        "msgs, _ := ch.Consume(\"go.queue\", \"\", true, false, false, false, nil)\n"
    ), {("PRODUCES", "Queue:sqs:go-sqs-out"), ("CONSUMES", "Queue:sqs:go-sqs-in"),
        ("PRODUCES", "Exchange:rabbitmq:go.ex"), ("PRODUCES", "Exchange:rabbitmq:go.ex2"),
        ("PRODUCES", "Queue:rabbitmq:go.default"), ("CONSUMES", "Queue:rabbitmq:go.queue")},
        id="go-sqs-amqp"),
    pytest.param(".java", (
        "class A {\n"
        '  void f() { rabbitTemplate.convertAndSend("j.ex", "rk", msg); }\n'
        '  void g() { rabbitTemplate.convertAndSend("j.queue", msg); }\n'
        '  @RabbitListener(queues = "j.listen") void h(String m) {}\n'
        '  @RabbitListener(queuesToDeclare = @Queue("j.declared")) void i(String m) {}\n'
        '  @RabbitListener(queues = {"j.a", "${rabbit.q}"}) void k(String m) {}\n}\n'
    ), {("PRODUCES", "Exchange:rabbitmq:j.ex"), ("PRODUCES", "Queue:rabbitmq:j.queue"),
        ("CONSUMES", "Queue:rabbitmq:j.listen"), ("CONSUMES", "Queue:rabbitmq:j.declared"),
        ("CONSUMES", "Queue:rabbitmq:j.a"), ("CONSUMES", "Queue:rabbitmq:from.props")},
        id="spring-amqp"),
    pytest.param(".java", (
        "class A {\n"
        '  void f() { jmsTemplate.convertAndSend("jms.one", msg); }\n'
        '  void g() { jmsTemplate.send("jms.two", creator); }\n'
        '  @JmsListener(destination = "jms.three") void h(String m) {}\n}\n'
    ), {("PRODUCES", "Queue:jms:jms.one"), ("PRODUCES", "Queue:jms:jms.two"),
        ("CONSUMES", "Queue:jms:jms.three")}, id="jms"),
    pytest.param(".java", (
        "class A {\n"
        '  @SqsListener("sl.one") void a(String m) {}\n'
        '  @SqsListener(value = "${sqs.q}") void b(String m) {}\n'
        '  void c() { sqsTemplate.send("sl.send", payload); }\n'
        '  void d() { sqsTemplate.send(to -> to.queue("sl.lambda").payload(p)); }\n'
        "  void e() { SendMessageRequest.builder().queueUrl(\n"
        '      "https://sqs.us-east-1.amazonaws.com/1/aws2-out").messageBody("x").build(); }\n'
        '  void f() { ReceiveMessageRequest.builder().queueUrl("aws2-in").build(); }\n}\n'
    ), {("CONSUMES", "Queue:sqs:sl.one"), ("CONSUMES", "Queue:sqs:from.props"),
        ("PRODUCES", "Queue:sqs:sl.send"), ("PRODUCES", "Queue:sqs:sl.lambda"),
        ("PRODUCES", "Queue:sqs:aws2-out"), ("CONSUMES", "Queue:sqs:aws2-in")},
        id="spring-cloud-aws"),
]


@pytest.mark.parametrize(("suffix", "text", "expected"), RULE_CASES)
def test_rule_table_cases(tmp_path: Path, suffix: str, text: str, expected: set) -> None:
    manifest = _snippet(
        tmp_path, suffix, text,
        **{"src/main/resources/application.properties": "rabbit.q=from.props\nsqs.q=from.props\n"})
    assert _flows(manifest) == expected
    assert all(src == "Service:svc" for (src, _t, _d) in _edges(manifest, "PRODUCES", "CONSUMES"))


def test_kotlin_and_scala_use_the_jvm_rules(tmp_path: Path) -> None:
    manifest = _snippet(tmp_path, ".kt", (
        "class A(private val rabbitTemplate: RabbitTemplate) {\n"
        '  @RabbitListener(queues = ["kt.q"]) fun a(m: String) {}\n'
        '  fun b() { rabbitTemplate.convertAndSend("kt.ex", "rk", "m") }\n}\n'))
    assert _flows(manifest) == {("CONSUMES", "Queue:rabbitmq:kt.q"),
                                ("PRODUCES", "Exchange:rabbitmq:kt.ex")}


def test_queue_and_exchange_entities_and_attributes(tmp_path: Path) -> None:
    manifest = _snippet(tmp_path, ".ts", (
        "import { SendMessageCommand } from '@aws-sdk/client-sqs'\n"
        "new SendMessageCommand({ QueueUrl: 'https://sqs.us-east-1.amazonaws.com/123/orders.fifo' })\n"
        "new PublishCommand({ TopicArn: 'arn:aws:sns:us-east-1:123:order-events' })\n"))
    queue = _entity(manifest, "Queue", "sqs:orders.fifo")
    assert queue.attributes["broker"] == "sqs" and queue.attributes["name"] == "orders.fifo"
    assert queue.attributes["url"] == "https://sqs.us-east-1.amazonaws.com/123/orders.fifo"
    exchange = _entity(manifest, "Exchange", "sns:order-events")
    assert exchange.attributes["arn"] == "arn:aws:sns:us-east-1:123:order-events"
    assert exchange.attributes["broker"] == "sns"


def test_dest_name_from_url_and_arn() -> None:
    assert msg.dest_name("https://sqs.eu-west-1.amazonaws.com/1/my-q") == (
        "my-q", {"url": "https://sqs.eu-west-1.amazonaws.com/1/my-q"})
    assert msg.dest_name("arn:aws:sqs:eu-west-1:1:my-q") == ("my-q", {"arn": "arn:aws:sqs:eu-west-1:1:my-q"})
    assert msg.dest_name("plain") == ("plain", {})


def test_unresolved_destinations_are_candidates_for_every_broker(tmp_path: Path) -> None:
    manifest = _snippet(tmp_path, ".ts", (
        "import amqp from 'amqplib'\n"
        "ch.sendToQueue(queueName, Buffer.from('x'))\n"
        "new ReceiveMessageCommand({ QueueUrl: config.url })\n"
        "ch.publish(process.env.EXCHANGE, 'k', b)\n"))
    queue = _entity(manifest, "Queue", "rabbitmq:unresolved:queueName")
    assert queue.attributes["unresolved"] is True and queue.attributes["expression"] == "queueName"
    assert _entity(manifest, "Queue", "sqs:unresolved:config.url")
    assert _entity(manifest, "Exchange", "rabbitmq:unresolved:env:EXCHANGE")
    assert {r.status for r in _edges(manifest).values()} == {"candidate"}


def test_unresolved_stays_candidate_in_verified_mode(tmp_path: Path) -> None:
    root = _write(tmp_path / "repo", {
        **SCAFFOLD[".ts"], "svc/a.ts": "import 'amqplib'\nch.sendToQueue(q, b)\nch.consume('lit', f)\n"})
    manifest = scan_repository(root, heuristics="verified").manifest
    statuses = {r.dst: r.status for r in _edges(manifest).values()}
    assert statuses == {"Queue:rabbitmq:unresolved:q": "candidate", "Queue:rabbitmq:lit": "verified"}


# -- generic constant index ------------------------------------------------------------


def test_js_constants_objects_enums_and_env_defaults(tmp_path: Path) -> None:
    manifest = _snippet(tmp_path, ".ts", (
        "import { Kafka } from 'kafkajs'\n"
        "import { Topics, T, ENV_TOPIC, PLAIN } from './topics'\n"
        "await producer.send({ topic: Topics.ORDERS, messages })\n"
        "await producer.send({ topic: T.PAID, messages })\n"
        "await producer.send({ topic: ENV_TOPIC, messages })\n"
        "await producer.send({ topic: PLAIN, messages })\n"
        "await producer.send({ topic: process.env.PAY_TOPIC ?? 'pay.default', messages })\n"
        "await producer.send({ topic: process.env['X_TOPIC'] || \"x.default\", messages })\n"
        "await producer.send({ topic: `${PLAIN}.suffix`, messages })\n"
        "await producer.send({ topic: PREFIX + '.cat', messages })\n"
        "await producer.send({ topic: process.env.NO_DEFAULT, messages })\n"),
        **{"src/topics.ts": (
            "export const Topics = {\n  ORDERS: 'orders.obj',\n  'DASH': \"dash.obj\",\n} as const\n"
            "export enum T { PAID = 'paid.enum', B = \"b.enum\" }\n"
            "export const ENV_TOPIC = process.env.ENV_T ?? 'env.const'\n"
            "export const PLAIN: string = \"plain.const\"\nlet PREFIX = 'pre'\n")})
    keys = {e.key for e in manifest.entities if e.type == "KafkaTopic"}
    assert keys == {
        "orders.obj", "paid.enum", "env.const", "plain.const", "pay.default", "x.default",
        "plain.const.suffix", "pre.cat", "unresolved:env:NO_DEFAULT"}
    pay = _entity(manifest, "KafkaTopic", "pay.default")
    assert pay.attributes["resolved_from"] == "env_default" and pay.attributes["env"] == "PAY_TOPIC"
    env_const = _entity(manifest, "KafkaTopic", "env.const")
    assert env_const.attributes["env"] == "ENV_T"
    assert _entity(manifest, "KafkaTopic", "orders.obj").attributes["resolved_from"] == "constant:ORDERS"


def test_python_constants_and_env(tmp_path: Path) -> None:
    manifest = _snippet(tmp_path, ".py", (
        "import os\nfrom kafka import KafkaConsumer\n"
        "producer.send(MODULE_TOPIC, b'x')\n"
        "producer.send(Topics.PAY, b'x')\n"
        "producer.send(self.audit_topic, b'x')\n"
        "producer.send(os.getenv('PY_TOPIC', 'py.env'), b'x')\n"
        "producer.send(os.environ.get(\"PY2_TOPIC\", \"py.env2\"), b'x')\n"
        "producer.send(os.getenv('PY_NONE'), b'x')\n"
        "sqs.send_message(QueueUrl=QUEUE_URL, MessageBody='x')\n"),
        **{"src/consts.py": (
            "MODULE_TOPIC = 'py.module'\nQUEUE_URL: str = 'https://sqs.us-east-1.amazonaws.com/1/py-const-q'\n"
            "class Topics:\n    PAY = \"py.pay\"\n    def __init__(self):\n"
            "        self.audit_topic = 'py.audit'\n")})
    assert {e.key for e in manifest.entities if e.type == "KafkaTopic"} == {
        "py.module", "py.pay", "py.audit", "py.env", "py.env2", "unresolved:env:PY_NONE"}
    assert _entity(manifest, "Queue", "sqs:py-const-q")
    assert _entity(manifest, "KafkaTopic", "py.env").attributes["env"] == "PY_TOPIC"


def test_go_constants(tmp_path: Path) -> None:
    manifest = _snippet(tmp_path, ".go", (
        "package main\nimport kafka \"github.com/segmentio/kafka-go\"\n"
        "const (\n\tTopicOrders = \"go.orders\"\n\tTopicPay string = \"go.pay\"\n)\n"
        "const single = \"go.single\"\n"
        "var a = &kafka.Writer{Topic: TopicOrders}\nvar b = &kafka.Writer{Topic: TopicPay}\n"
        "var c = &kafka.Writer{Topic: single}\nvar d = &kafka.Writer{Topic: os.Getenv(\"T\")}\n"))
    assert {e.key for e in manifest.entities if e.type == "KafkaTopic"} == {
        "go.orders", "go.pay", "go.single", "unresolved:env:T"}


def test_extract_constants_directly() -> None:
    js = msg.extract_constants(
        "export const A = 'a'\nconst B = process.env.B ?? \"b\"\nconst C = 'x' + y\n"
        "export const O = { K: 'k', L: 1 }\nenum E { X = 'x' }\n", ".ts")
    assert (None, "A", "a", None) in js and (None, "B", "b", "B") in js
    assert ("O", "K", "k", None) in js and ("E", "X", "x", None) in js
    assert not any(item[1] == "C" for item in js)
    py = msg.extract_constants("X = 'x'\nclass T:\n    A = 'a'\n    def f(self):\n        z = 'no'\n", ".py")
    assert py == [(None, "X", "x", None), ("T", "A", "a", None)]
    go = msg.extract_constants("const X = \"x\"\nvar (\n\tY = \"y\"\n)\n", ".go")
    assert go == [(None, "X", "x", None), (None, "Y", "y", None)]


def test_jvm_rules_use_the_spring_resolver(tmp_path: Path) -> None:
    manifest = _snippet(
        tmp_path, ".java", (
            "class A {\n  static final String Q = \"const.queue\";\n"
            '  @Value("${amqp.exchange}") String exchange;\n'
            '  void f() { rabbitTemplate.convertAndSend(exchange, Q, m); }\n'
            '  void g() { jmsTemplate.send(Q, c); }\n'
            '  void h() { jmsTemplate.send("${missing.prop}", c); }\n}\n'),
        **{"src/main/resources/application.yml": "amqp:\n  exchange: from.yaml\n"})
    assert _flows(manifest) == {
        ("PRODUCES", "Exchange:rabbitmq:from.yaml"), ("PRODUCES", "Queue:jms:const.queue"),
        ("PRODUCES", "Queue:jms:unresolved:missing.prop")}
    assert _entity(manifest, "Exchange", "rabbitmq:from.yaml").attributes["resolved_from"] == (
        "property:amqp.exchange")


def test_comments_and_unrelated_calls_are_ignored(tmp_path: Path) -> None:
    manifest = _snippet(tmp_path, ".ts", (
        "// await producer.send({ topic: 'commented', messages })\n"
        "import 'kafkajs'\nconst bus = new EventEmitter(); bus.publish('not-amqp', x)\n"
        "other.send({ topic: 'x' })\n"))
    assert _flows(manifest) == set()


# -- C: Kafka producers on the JVM -----------------------------------------------------


def _spring(tmp_path: Path, files: dict[str, str], dockerfile: bool = True) -> Path:
    base = {"svc/build.gradle": "plugins { id 'java' }\n"}
    if dockerfile:
        base["svc/Dockerfile"] = "FROM x\n"
    return _write(tmp_path / "repo", {**base, **{f"svc/{k}": v for k, v in files.items()}})


JAVA = "src/main/java/com/acme/K.java"


def test_kafka_producer_receivers(tmp_path: Path) -> None:
    root = _spring(tmp_path, {JAVA: (
        "class K {\n"
        '  void a() { kafkaTemplate.send("t.template", v); }\n'
        '  void b() { eventProducer.send("t.producer", v); }\n'
        '  void c() { messageSender.send("t.sender", v); }\n'
        '  void d() { producer.produce("t.produce", v); }\n'
        '  void e() { restTemplate.send("t.rest", v); mailSender.send("t.mail"); }\n'
        '  void f() { rabbitTemplate.send("t.rabbit", v); redisTemplate.send("t.redis", v); }\n'
        '  void g() { var r = new ProducerRecord<String, String>("t.record", v); }\n'
        '  void h() { kafkaSender.send(Mono.just(SenderRecord.create("t.reactor", 0, 1L, k, v, null))); }\n'
        "}\n")})
    manifest = scan_repository(root).manifest
    produced = {r.dst for r in _edges(manifest, "PRODUCES").values() if r.dst.startswith("KafkaTopic")}
    assert produced == {f"KafkaTopic:t.{name}" for name in (
        "template", "producer", "sender", "produce", "record", "reactor")}


def test_send_default_topic_from_properties_or_setter(tmp_path: Path) -> None:
    root = _spring(tmp_path, {
        "src/main/resources/application.properties": "spring.kafka.template.default-topic=def.topic\n",
        JAVA: "class K { void a() { kafkaTemplate.sendDefault(v); kafkaTemplate.sendDefault(k, v); } }\n",
    })
    manifest = scan_repository(root).manifest
    assert _flows(manifest) == {("PRODUCES", "KafkaTopic:def.topic")}
    root = _spring(tmp_path / "b", {
        JAVA: ('class K {\n  KafkaTemplate t; static final String D = "set.default";\n'
               "  void init() { t.setDefaultTopic(D); }\n"
               "  void a() { kafkaTemplate.sendDefault(v); }\n}\n"),
    })
    assert _flows(scan_repository(root).manifest) == {("PRODUCES", "KafkaTopic:set.default")}
    root = _spring(tmp_path / "c", {JAVA: "class K { void a() { kafkaTemplate.sendDefault(v); } }\n"})
    result = scan_repository(root)
    assert _flows(result.manifest) == {("PRODUCES", "KafkaTopic:unresolved:sendDefault")}
    assert result.summary["kafka"]["unresolved_placeholders"] == 1


def test_stream_bridge_destination_binding_and_binder(tmp_path: Path) -> None:
    root = _spring(tmp_path, {
        "src/main/resources/application.yml": (
            "spring:\n  cloud:\n    stream:\n      bindings:\n"
            "        orders-out-0:\n          destination: orders.bridge\n"
            "        wire-out-0:\n          destination: wire.exchange\n          binder: rabbit1\n"),
        JAVA: ('class K {\n  void a() { streamBridge.send("orders-out-0", p); }\n'
               '  void b() { streamBridge.send("direct.topic", p); }\n'
               '  void c() { streamBridge.send("wire-out-0", p); }\n}\n'),
    })
    result = scan_repository(root)
    assert _flows(result.manifest) == {
        ("PRODUCES", "KafkaTopic:orders.bridge"), ("PRODUCES", "KafkaTopic:direct.topic"),
        ("PRODUCES", "Exchange:rabbitmq:wire.exchange")}
    assert _entity(result.manifest, "KafkaTopic", "orders.bridge").attributes["binding"] == "orders-out-0"
    assert result.summary["messaging"]["rabbitmq"]["producer_sites"] == 1
    assert result.summary["messaging"]["kafka"]["producer_sites"] == 2


def test_send_to_on_a_kafka_listener(tmp_path: Path) -> None:
    root = _spring(tmp_path, {
        "src/main/resources/application.properties": "out.t=out.resolved\n",
        JAVA: ('class K {\n  @KafkaListener(topics = "in.t")\n  @SendTo("out.t.literal")\n'
               "  String a(String m) { return m; }\n"
               '  @SendTo("${out.t}")\n  @KafkaListener(topics = "in.2")\n'
               "  String b(String m) { return m; }\n"
               '  @SendTo("not.kafka")\n  String c(String m) { return m; }\n}\n')})
    result = scan_repository(root)
    assert _flows(result.manifest) == {
        ("CONSUMES", "KafkaTopic:in.t"), ("CONSUMES", "KafkaTopic:in.2"),
        ("PRODUCES", "KafkaTopic:out.t.literal"), ("PRODUCES", "KafkaTopic:out.resolved")}


def test_binding_destinations_resolve_placeholders_and_lists(tmp_path: Path) -> None:
    root = _spring(tmp_path, {
        "src/main/resources/application.properties": (
            "env.name=prod\n"
            "spring.cloud.stream.bindings.a-in-0.destination=${env.name}.orders,${env.name}.pay\n"
            "spring.cloud.stream.bindings.b-out-0.destination=${missing.dest}\n"),
        "src/main/resources/application-extra.yml": (
            "spring:\n  cloud:\n    stream:\n      bindings:\n        c-out-0:\n"
            "          destination: \"${env.name}.audit\"\n")})
    result = scan_repository(root)
    assert _flows(result.manifest) == {
        ("CONSUMES", "KafkaTopic:prod.orders"), ("CONSUMES", "KafkaTopic:prod.pay"),
        ("PRODUCES", "KafkaTopic:unresolved:missing.dest"), ("PRODUCES", "KafkaTopic:prod.audit")}


# -- D: SpEL ----------------------------------------------------------------------------


def _spel(tmp_path: Path, expr: str, files: dict[str, str] | None = None, body: str = ""):
    java = ("class K {\n" + body + '  @KafkaListener(topics = "' + expr.replace('"', '\\"')
            + '") void a() {}\n}\n')
    root = _spring(tmp_path, {JAVA: java, **(files or {})})
    return scan_repository(root)


def _topics(result) -> set[str]:
    return {e.key for e in result.manifest.entities if e.type == "KafkaTopic"}


PROPS = {"src/main/resources/application.properties": (
    "a.b=one,two\nsingle=solo\nb=suffix\n")}


@pytest.mark.parametrize(("expr", "expected"), [
    ("#{'${a.b}'}", {"one", "two"}),
    ("#{'${a.b}'.split(',')}", {"one", "two"}),
    ("#{'literal'}", {"literal"}),
    ("#{'a' + '${b}'}", {"asuffix"}),
    ("pre.#{'${single}'}.post", {"pre.solo.post"}),
    ("#{environment['single']}", {"solo"}),
    ("#{environment.getProperty('single')}", {"solo"}),
    ("#{environment.getProperty('nope','dflt')}", {"dflt"}),
    ("#{environment.getProperty('a.b').split(',')}", {"one", "two"}),
    ("#{systemProperties['single']}", {"solo"}),
    ("#{T(com.acme.Topics).ORDERS}", {"orders.const"}),
    ("#{T(Topics).ORDERS + '.v2'}", {"orders.const.v2"}),
])
def test_spel_forms(tmp_path: Path, expr: str, expected: set[str]) -> None:
    files = {**PROPS, "src/main/java/com/acme/Topics.java": (
        'class Topics { public static final String ORDERS = "orders.const"; }\n')}
    result = _spel(tmp_path, expr, files)
    assert _topics(result) == expected
    topic = _entity(result.manifest, "KafkaTopic", sorted(expected)[0])
    assert topic.attributes["resolved_from"] == "spel" and topic.attributes["spel"] == expr


@pytest.mark.parametrize("expr", [
    "#{'${missing}'.split(',')}", "#{environment.getProperty('nope')}",
    "#{T(Nope).X}", "#{1 + 2}", "#{some.bean.call()}",
])
def test_spel_unresolved_forms(tmp_path: Path, expr: str) -> None:
    result = _spel(tmp_path, expr, PROPS)
    (key,) = _topics(result)
    topic = _entity(result.manifest, "KafkaTopic", key)
    assert key.startswith("unresolved:") and topic.attributes["unresolved"] is True
    assert topic.attributes["spel"] == expr


def test_spel_unresolved_keeps_expanded_placeholders(tmp_path: Path) -> None:
    result = _spel(tmp_path, "#{@x.y('${single}')} ${nope}", PROPS)
    (key,) = _topics(result)
    assert key == "unresolved:#{@x.y('solo')} ${nope}"


def test_spel_beans_and_listener(tmp_path: Path) -> None:
    root = _spring(tmp_path, {
        "src/main/resources/application.properties": "bean.topic=from.bean\nown.topic=from.own\n",
        "src/main/java/com/acme/TopicConfig.java": (
            'public class TopicConfig {\n  @Value("${bean.topic}") private String orders;\n'
            '  public static final String AUDIT = "bean.const";\n  String getOrders() { return orders; }\n}\n'),
        JAVA: (
            'class K {\n  @Value("${own.topic}") String topic;\n'
            '  @KafkaListener(topics = "#{@topicConfig.orders}") void a() {}\n'
            '  @KafkaListener(topics = "#{@topicConfig.getOrders()}") void b() {}\n'
            '  @KafkaListener(topics = "#{@topicConfig.AUDIT}") void c() {}\n'
            '  @KafkaListener(topics = "#{__listener.topic}") void d() {}\n'
            '  @KafkaListener(topics = "#{@ghostBean.topic}") void e() {}\n}\n')})
    result = scan_repository(root)
    assert _topics(result) == {
        "from.bean", "bean.const", "from.own", "unresolved:#{@ghostBean.topic}"}
    ghost = _entity(result.manifest, "KafkaTopic", "unresolved:#{@ghostBean.topic}")
    assert ghost.attributes["bean"] == "ghostBean" and ghost.attributes["spel"] == "#{@ghostBean.topic}"


# -- E: service attribution --------------------------------------------------------------


def _orders_repo(tmp_path: Path) -> Path:
    return _write(tmp_path / "shop", {
        "settings.gradle": "include ':app', ':messaging'\n",
        "build.gradle": "// root\n",
        "app/build.gradle.kts": (
            'plugins { id("org.springframework.boot") version "3.2.0" }\n'
            'dependencies { implementation(project(":messaging")) }\n'),
        "app/src/main/resources/application.yml": (
            "spring:\n  application:\n    name: orders-service\norders:\n  topics: orders.created,orders.updated\n"),
        "app/src/main/java/com/acme/App.java": "@SpringBootApplication class App {}\n",
        "messaging/build.gradle": "plugins { id 'java-library' }\n",
        "messaging/src/main/java/com/acme/Topics.java": (
            'public class Topics { public static final String AUDIT = "audit.events"; }\n'),
        "messaging/src/main/java/com/acme/Publisher.java": (
            "class Publisher {\n  void send() { kafkaTemplate.send(Topics.AUDIT, payload); }\n}\n"),
        "messaging/src/main/java/com/acme/Listener.java": (
            "class Listener {\n"
            "  @KafkaListener(topics = \"#{'${orders.topics}'.split(',')}\")\n  void on(String m) {}\n}\n"),
    })


def test_library_messaging_is_attributed_to_the_depending_service(tmp_path: Path) -> None:
    result = scan_repository(_orders_repo(tmp_path))
    manifest = result.manifest
    assert _keys(manifest, "Service") == {"orders-service"}
    service = _entity(manifest, "Service", "orders-service")
    assert service.attributes["detected_by"] == "spring_boot_plugin"
    edges = _edges(manifest)
    assert set(edges) == {
        ("Service:orders-service", "PRODUCES", "KafkaTopic:audit.events"),
        ("Service:orders-service", "CONSUMES", "KafkaTopic:orders.created"),
        ("Service:orders-service", "CONSUMES", "KafkaTopic:orders.updated"),
    }
    assert all(e.attributes == {"via_module": "messaging"} for e in edges.values())
    assert _entity(manifest, "KafkaTopic", "orders.created").attributes["resolved_from"] == "spel"
    assert ("Module:app", "BUILDS", "Service:orders-service") in {
        (r.src, r.type, r.dst) for r in manifest.relations}
    assert result.summary["attribution"] == {
        "edges_from_service": 0, "edges_via_dependency": 3, "edges_from_module": 0}


def _keys(manifest, entity_type: str) -> set[str]:
    return {e.key for e in manifest.entities if e.type == entity_type}


def test_schema_v1_attributes_through_service_dependencies(tmp_path: Path) -> None:
    manifest = scan_repository(_orders_repo(tmp_path), schema_version=1).manifest
    assert ("Service:orders-service", "PRODUCES", "KafkaTopic:audit.events") in _edges(manifest)


def test_edges_from_module_when_nothing_deployable_depends_on_it(tmp_path: Path) -> None:
    root = _write(tmp_path / "r", {
        "lib/build.gradle": "plugins { id 'java-library' }\n",
        "lib/K.java": 'class K { void f() { kafkaTemplate.send("lib.topic", v); } }\n'})
    result = scan_repository(root)
    assert set(_edges(result.manifest)) == {("Module:lib", "PRODUCES", "KafkaTopic:lib.topic")}
    assert result.summary["attribution"]["edges_from_module"] == 1
    assert scan_repository(root, schema_version=1).summary["kafka"]["skipped_no_owner"] == 1


def test_several_services_depend_on_a_library_and_dedupe(tmp_path: Path) -> None:
    root = _write(tmp_path / "r", {
        "settings.gradle": "include ':a', ':b', ':lib', ':mid'\n",
        "a/build.gradle": "plugins { id 'application' }\ndependencies { implementation project(':mid') }\n",
        "b/build.gradle": "plugins { id 'org.springframework.boot' }\ndependencies { api project(':lib') }\n",
        "mid/build.gradle": "dependencies { api project(':lib') }\n",
        "lib/build.gradle": "plugins { id 'java-library' }\n",
        "lib/K.java": ('class K {\n  void f() { kafkaTemplate.send("lib.t", v); }\n'
                       '  void g() { kafkaTemplate.send("lib.t", v); }\n}\n')})
    result = scan_repository(root)
    assert set(_edges(result.manifest)) == {
        ("Service:a", "PRODUCES", "KafkaTopic:lib.t"), ("Service:b", "PRODUCES", "KafkaTopic:lib.t")}
    assert result.summary["attribution"]["edges_via_dependency"] == 2
    assert all(e.attributes["via_module"] == "lib" for e in _edges(result.manifest).values())


@pytest.mark.parametrize(("files", "detected", "name"), [
    ({"m/build.gradle": "plugins { id 'application' }\n"}, "gradle_application_plugin", "m"),
    ({"m/build.gradle": "application { mainClass = 'x.Main' }\n"}, "gradle_main_class", "m"),
    ({"m/build.gradle": "bootJar { archiveName 'x' }\n"}, "gradle_main_class", "m"),
    ({"m/pom.xml": ("<project><groupId>g</groupId><artifactId>art-svc</artifactId><build><plugins>"
                    "<plugin><artifactId>spring-boot-maven-plugin</artifactId></plugin>"
                    "</plugins></build></project>")}, "maven_spring_boot_plugin", "art-svc"),
    ({"m/build.gradle": "plugins { id 'java' }\n",
      "m/src/App.java": "@SpringBootApplication public class App {}\n"},
     "spring_boot_application", "m"),
    ({"m/build.gradle": "plugins { id 'java' }\n",
      "m/src/Main.java": "class Main { public static void main(String[] a) {} }\n"},
     "main_function", "m"),
    ({"m/build.gradle": "plugins { id 'java' }\n", "m/src/Main.kt": "fun main(args: Array<String>) {}\n"},
     "main_function", "m"),
    ({"m/package.json": '{"name": "@acme/web", "scripts": {"start": "node ."}}'},
     "package_json_start", "@acme/web"),
    ({"m/package.json": '{"name": "cli-tool", "bin": {"cli": "bin.js"}}'}, "package_json_bin", "cli-tool"),
    ({"m/go.mod": "module github.com/acme/goapp\n", "m/cmd.go": "package main\nfunc main() {}\n"},
     "go_package_main", "goapp"),
    ({"m/pyproject.toml": '[project]\nname = "pyapp"\n[project.scripts]\npyapp = "pyapp:main"\n'},
     "python_scripts", "pyapp"),
    ({"m/pyproject.toml": '[project]\nname = "pyapp"\n', "m/pyapp/__main__.py": "print(1)\n"},
     "python_main_module", "pyapp"),
    ({"m/pyproject.toml": '[project]\nname = "pyapp"\n', "m/Procfile": "web: python app.py\n"},
     "procfile", "pyapp"),
])
def test_deployable_detection_without_a_dockerfile(
    tmp_path: Path, files: dict[str, str], detected: str, name: str,
) -> None:
    manifest = scan_repository(_write(tmp_path / "r", files)).manifest
    (service,) = [e for e in manifest.entities if e.type == "Service"]
    assert service.key == name and service.attributes["detected_by"] == detected


@pytest.mark.parametrize("files", [
    {"m/build.gradle": "plugins { id 'org.springframework.boot' version '3' apply false }\n"},
    {"m/build.gradle": "plugins { id 'org.springframework.boot' }\nbootJar { enabled = false }\n"},
    {"m/build.gradle": "dependencies { implementation 'org.springframework.boot:spring-boot-starter' }\n"},
    {"m/pom.xml": ("<project><artifactId>x</artifactId><packaging>pom</packaging><build><plugins>"
                   "<plugin><artifactId>spring-boot-maven-plugin</artifactId></plugin>"
                   "</plugins></build></project>")},
    {"m/pom.xml": ("<project><artifactId>x</artifactId><build><pluginManagement><plugins>"
                   "<plugin><artifactId>spring-boot-maven-plugin</artifactId></plugin>"
                   "</plugins></pluginManagement></build></project>")},
    {"m/go.mod": "module example.com/lib\n", "m/lib.go": "package lib\n"},
    {"m/go.mod": "module example.com/lib\n", "m/lib_test.go": "package main\n"},
])
def test_libraries_are_not_services(tmp_path: Path, files: dict[str, str]) -> None:
    manifest = scan_repository(_write(tmp_path / "r", files)).manifest
    assert not [e for e in manifest.entities if e.type == "Service"]


def test_service_name_precedence(tmp_path: Path) -> None:
    root = _write(tmp_path / "r", {
        # chart > spring name
        "a/build.gradle": "plugins { id 'org.springframework.boot' }\n",
        "a/Chart.yaml": "name: chart-a\n",
        "a/src/main/resources/application.properties": "spring.application.name=spring-a\n",
        # k8s workload > spring name
        "b/build.gradle": "plugins { id 'org.springframework.boot' }\n",
        "b/k8s.yaml": "kind: Deployment\nmetadata:\n  name: k8s-b\n",
        "b/src/main/resources/application.yml": "spring:\n  application:\n    name: spring-b\n",
        # Dockerfile (directory name) < spring name; env default accepted
        "c/build.gradle": "plugins { id 'java' }\n",
        "c/Dockerfile": "FROM x\n",
        "c/src/main/resources/application.yml": "spring:\n  application:\n    name: ${APP_NAME:spring-c}\n",
        # spring name < nothing: only a module name
        "d/build.gradle": "plugins { id 'application' }\n",
    })
    manifest = scan_repository(root).manifest
    by_module = {e.attributes["module"]: e for e in manifest.entities if e.type == "Service"}
    assert {m: (s.key, s.attributes["detected_by"]) for m, s in by_module.items()} == {
        "a": ("chart-a", "chart"), "b": ("k8s-b", "k8s"), "c": ("spring-c", "dockerfile"),
        "d": ("d", "gradle_application_plugin")}


def test_application_profile_names_do_not_beat_the_base_file(tmp_path: Path) -> None:
    root = _write(tmp_path / "r", {
        "m/build.gradle": "plugins { id 'application' }\n",
        "m/src/main/resources/application-dev.properties": "spring.application.name=dev-name\n",
        "m/src/main/resources/application.properties": "spring.application.name=base-name\n"})
    manifest = scan_repository(root).manifest
    assert _keys(manifest, "Service") == {"base-name"}


# -- E: Node repos -----------------------------------------------------------------------

NODE = {
    "package.json": json.dumps({"scripts": {"start": "node dist/index.js"},
                                "dependencies": {"kafkajs": "^2", "amqplib": "^0.10"}}),
    "src/topics.ts": (
        "export const Topics = { ORDERS: 'orders', PAYMENTS: 'payments' }\n"
        "export const QUEUE_URL = process.env.JOBS_QUEUE_URL ?? "
        "'https://sqs.us-east-1.amazonaws.com/123/jobs'\n"),
    "src/events.ts": (
        "import { Kafka } from 'kafkajs'\nimport amqp from 'amqplib'\n"
        "import { SQSClient, SendMessageCommand } from '@aws-sdk/client-sqs'\n"
        "import { Topics, QUEUE_URL } from './topics'\n"
        "export async function run(producer, consumer, ch, sqs) {\n"
        "  await consumer.subscribe({ topics: [Topics.ORDERS] })\n"
        "  await producer.send({ topic: Topics.PAYMENTS, messages: [] })\n"
        "  ch.sendToQueue('emails', Buffer.from('x'))\n"
        "  await sqs.send(new SendMessageCommand({ QueueUrl: QUEUE_URL, MessageBody: 'x' }))\n}\n"),
}


def test_node_repo_without_package_name_or_dockerfile(tmp_path: Path) -> None:
    root = _write(tmp_path / "web-shop", NODE)
    result = scan_repository(root)
    manifest = result.manifest
    assert _keys(manifest, "Module") == {"(root)"}
    assert _entity(manifest, "Module", "(root)").attributes["name"] == "web-shop"
    service = _entity(manifest, "Service", "web-shop")
    assert service.attributes["detected_by"] == "package_json_start"
    assert set(_edges(manifest)) == {
        ("Service:web-shop", "CONSUMES", "KafkaTopic:orders"),
        ("Service:web-shop", "PRODUCES", "KafkaTopic:payments"),
        ("Service:web-shop", "PRODUCES", "Queue:rabbitmq:emails"),
        ("Service:web-shop", "PRODUCES", "Queue:sqs:jobs"),
    }
    assert _entity(manifest, "Queue", "sqs:jobs").attributes["env"] == "JOBS_QUEUE_URL"
    assert result.summary["messaging"]["sqs"]["skipped_no_owner"] == 0
    assert result.summary["warnings"] == []


def test_node_package_without_name_in_a_subdirectory(tmp_path: Path) -> None:
    root = _write(tmp_path / "mono", {
        "packages/api/package.json": '{"private": true}',
        "packages/api/src/a.ts": "import 'amqplib'\nch.consume('api.q', f)\n",
        "package.json": '{"private": true, "workspaces": ["packages/*"]}'})
    manifest = scan_repository(root).manifest
    assert _keys(manifest, "Module") == {"packages/api"}  # the nameless workspace root is not a module
    assert _entity(manifest, "Module", "packages/api").attributes["name"] == "api"
    assert ("Module:packages/api", "CONSUMES", "Queue:rabbitmq:api.q") in _edges(manifest)


def test_node_source_only_repo_gets_a_synthetic_root_module(tmp_path: Path) -> None:
    root = _write(tmp_path / "just-src", {
        "src/index.js": "const { Kafka } = require('kafkajs')\nawait producer.send({ topic: 'js.t', messages })\n"})
    result = scan_repository(root)
    assert _keys(result.manifest, "Module") == {"(root)"}
    assert set(_edges(result.manifest)) == {("Module:(root)", "PRODUCES", "KafkaTopic:js.t")}
    assert result.summary["kafka"]["skipped_no_owner"] == 0
    ref = [r for r in result.manifest.relations if r.type == "CONTAINS"]
    assert ref[0].attestation == "src/index.js#L1"


# -- (c) Python and Go mini repos ---------------------------------------------------------


def test_python_mini_repo(tmp_path: Path) -> None:
    root = _write(tmp_path / "pyrepo", {
        "pyproject.toml": '[project]\nname = "orders-worker"\n[project.scripts]\nworker = "w:main"\n',
        "src/w/topics.py": "import os\nORDERS = 'orders'\nJOBS = os.getenv('JOBS_QUEUE', 'jobs-q')\n",
        "src/w/main.py": (
            "from confluent_kafka import Producer, Consumer\nimport boto3, pika\nfrom w.topics import ORDERS, JOBS\n"
            "class W:\n    def run(self):\n        self._producer.produce(ORDERS, b'x')\n"
            "        consumer.subscribe([ORDERS])\n"
            "        sqs.receive_message(QueueUrl=JOBS)\n"
            "        ch.basic_publish(exchange='audit', routing_key='k', body=b'x')\n")})
    manifest = scan_repository(root).manifest
    assert _keys(manifest, "Service") == {"orders-worker"}
    assert set(_edges(manifest)) == {
        ("Service:orders-worker", "PRODUCES", "KafkaTopic:orders"),
        ("Service:orders-worker", "CONSUMES", "KafkaTopic:orders"),
        ("Service:orders-worker", "CONSUMES", "Queue:sqs:jobs-q"),
        ("Service:orders-worker", "PRODUCES", "Exchange:rabbitmq:audit")}


def test_go_mini_repo(tmp_path: Path) -> None:
    root = _write(tmp_path / "gorepo", {
        "go.mod": "module github.com/acme/ledger\n\ngo 1.22\n",
        "cmd/main.go": (
            "package main\nimport kafka \"github.com/segmentio/kafka-go\"\n"
            "const (\n\tTopicLedger = \"ledger.events\"\n\tQueueURL = \"https://sqs.us-east-1.amazonaws.com/1/ledger-q\"\n)\n"
            "func main() {\n\tw := &kafka.Writer{Topic: TopicLedger}\n"
            "\tr := kafka.NewReader(kafka.ReaderConfig{Topic: \"ledger.cmds\", GroupID: \"g\"})\n"
            "\tin := &sqs.ReceiveMessageInput{QueueUrl: aws.String(QueueURL)}\n}\n")})
    manifest = scan_repository(root).manifest
    assert _keys(manifest, "Service") == {"ledger"}
    assert set(_edges(manifest)) == {
        ("Service:ledger", "PRODUCES", "KafkaTopic:ledger.events"),
        ("Service:ledger", "CONSUMES", "KafkaTopic:ledger.cmds"),
        ("Service:ledger", "CONSUMES", "Queue:sqs:ledger-q")}
    assert _entity(manifest, "Service", "ledger").attributes["detected_by"] == "go_package_main"


# -- A: schema versions ------------------------------------------------------------------


def test_schema_below_3_skips_queue_and_exchange_sites(tmp_path: Path) -> None:
    root = _write(tmp_path / "r", {
        **SCAFFOLD[".ts"],
        "svc/a.ts": ("import 'kafkajs'\nimport 'amqplib'\n"
                     "producer.send({ topic: 'k.t', messages })\n"
                     "ch.sendToQueue('q1', b)\nch.publish('ex1', 'k', b)\n")})
    for version in (1, 2):
        result = scan_repository(root, schema_version=version)
        assert _flows(result.manifest) == {("PRODUCES", "KafkaTopic:k.t")}
        amqp = result.summary["messaging"]["rabbitmq"]
        assert amqp["skipped_schema"] == 2 and amqp["sites"] == 2 and amqp["matched_sites"] == 0
        (warning,) = [w for w in result.summary["warnings"] if "--schema-version 3" in w]
        assert "pack_versions = { infrastructure = 3 }" in warning
        assert not any("extracted 0" in w for w in result.summary["warnings"])
    result = scan_repository(root)
    assert result.manifest.schema_version == 3 and result.summary["schema_version"] == 3
    assert result.summary["messaging"]["rabbitmq"]["skipped_schema"] == 0
    assert not any("--schema-version 3" in w for w in result.summary["warnings"])


def test_invalid_schema_version(tmp_path: Path) -> None:
    with pytest.raises(OntologyError, match="1, 2 or 3"):
        scan_repository(tmp_path, schema_version=4)


def test_pack_v3_defines_queue_exchange_and_routes_to() -> None:
    registry = SchemaRegistry.builtin()
    assert registry.default_version("infrastructure") == 1  # opt-in: unchanged
    v3 = next(p for p in registry.packs() if p.id == "infrastructure" and p.version == 3)
    assert tuple(v3.entity_types["Queue"].required) == ("broker", "name")
    assert tuple(v3.entity_types["Exchange"].required) == ("broker", "name")
    assert tuple(v3.relation_types["ROUTES_TO"].targets) == ("Queue",)


def test_v3_manifest_seeds(tmp_path: Path) -> None:
    from ncp.config import load_config
    from ncp.ontology.facade import OntologyFacade
    from ncp.ontology.seed import memory_chunk_writer, seed_ontology
    from ncp.stores.sqlite import SQLiteStore

    root = _write(tmp_path / "r", {
        **SCAFFOLD[".ts"],
        "svc/a.ts": ("import 'kafkajs'\nimport 'amqplib'\nproducer.send({ topic: 'k.t' })\n"
                     "ch.sendToQueue('q1', b)\nch.bindQueue('q1', 'ex1', 'k')\n")})
    manifest = scan_repository(root, pipeline="shop").manifest
    assert {r.type for r in manifest.relations} >= {"PRODUCES", "ROUTES_TO"}
    project = tmp_path / "proj"
    (project / ".git").mkdir(parents=True)
    (project / ".ncp").mkdir()
    (project / ".ncp" / "config.toml").write_text(
        "[ontology]\nenabled = true\npack_versions = { infrastructure = 3 }\n", encoding="utf-8")
    store = SQLiteStore(project / ".ncp" / "store.db")
    facade = OntologyFacade.from_config(load_config(cwd=project), store)
    report = seed_ontology(facade, manifest, write_chunk=memory_chunk_writer(store))
    assert report.relations_created == len(manifest.relations)


# -- F: summary shape and CLI ------------------------------------------------------------


def test_summary_shape(tmp_path: Path) -> None:
    root = _write(tmp_path / "r", {
        **SCAFFOLD[".ts"],
        "svc/a.ts": ("import 'kafkajs'\nimport 'amqplib'\n"
                     "producer.send({ topic: 'k.t', messages })\n"
                     "consumer.subscribe({ topic: 'k.in' })\n"
                     "ch.sendToQueue('q1', b)\nch.consume(build(), f)\nch.sendToQueue(dynamic, b)\n")})
    summary = scan_repository(root).summary
    assert set(summary["messaging"]) == {"kafka", "rabbitmq"}
    amqp = summary["messaging"]["rabbitmq"]
    assert set(amqp) == {
        "sites", "producer_sites", "consumer_sites", "matched_sites", "unmatched_sites",
        "unresolved", "skipped_no_owner", "skipped_schema", "unmatched_examples"}
    assert (amqp["sites"], amqp["producer_sites"], amqp["consumer_sites"]) == (3, 2, 1)
    assert (amqp["matched_sites"], amqp["unresolved"], amqp["unmatched_sites"]) == (1, 1, 1)
    assert amqp["unmatched_examples"] == ["svc/a.ts:6: ch.consume(build(), f)"]
    kafka = summary["messaging"]["kafka"]
    assert (kafka["sites"], kafka["producer_sites"], kafka["consumer_sites"]) == (2, 1, 1)
    assert summary["kafka"]["kafka_like_sites"] == 2 and summary["kafka"]["producer_sites"] == 1
    assert summary["attribution"] == {
        "edges_from_service": 4, "edges_via_dependency": 0, "edges_from_module": 0}
    assert summary["destinations"] == {"KafkaTopic": 2, "Queue": 2, "Exchange": 0}


def test_warnings_cover_every_broker(tmp_path: Path) -> None:
    root = _write(tmp_path / "r", {
        **SCAFFOLD[".ts"],
        "svc/a.ts": "import 'amqplib'\nch.consume(build(), f)\nch.consume(other(), f)\n"})
    warnings = scan_repository(root).summary["warnings"]
    assert any("found 2 rabbitmq-like site(s) but extracted 0 destinations" in w for w in warnings)
    root = _write(tmp_path / "r2", {
        "lib/a.ts": "import 'amqplib'\nch.consume('q', f)\n"})
    warnings = scan_repository(root, schema_version=2).summary["warnings"]
    assert warnings and not any("skipped because no module" in w for w in warnings[1:])


def test_cli_prints_messaging_coverage_and_attribution(tmp_path: Path) -> None:
    root = _orders_repo(tmp_path)
    _write(root, {"messaging/src/main/java/com/acme/Q.java": (
        'class Q { void f() { jmsTemplate.send("jq", c); jmsTemplate.send(build(), c); } }\n')})
    result = CliRunner().invoke(main, ["ontology", "scan", str(root), "-o", str(tmp_path / "m.json")])
    assert result.exit_code == 0, result.output
    assert "Messaging coverage" in result.output and "jms" in result.output
    assert "edges from:" in result.output and "via dependency" in result.output
    assert "schema_version = 3" in result.output
    as_json = CliRunner().invoke(main, ["ontology", "scan", str(root), "--json"])
    payload = json.loads(as_json.output)
    assert payload["summary"]["messaging"]["jms"]["sites"] == 2
    assert payload["summary"]["attribution"]["edges_via_dependency"] >= 3
    assert payload["summary"]["destinations"]["Queue"] == 1
    choices = next(p for p in main.commands["ontology"].commands["scan"].params
                   if p.name == "schema_version").type.choices
    assert list(choices) == ["1", "2", "3"]
    old = CliRunner().invoke(main, ["ontology", "scan", str(root), "--schema-version", "2", "--json"])
    assert json.loads(old.output)["summary"]["messaging"]["jms"]["skipped_schema"] == 2


# -- linear time -------------------------------------------------------------------------

ADVERSARIAL_TOKENS = [
    "producer.send({", "producer.send({ topic: ", "consumer.subscribe({ topics: [", "sendBatch({ topicMessages: [{ ",
    "ch.publish(", "channel.consume(", "new SendMessageCommand({ ", "new ReceiveMessageCommand({{{",
    "Consumer.create({ ", "new PublishCommand({ ", "send_message(", "send_message((((", "receive_message(QueueUrl=",
    "sns.publish(", "basic_publish(", "KafkaConsumer(", "kafka.Writer{", "kafka.Writer{{{{",
    "ReaderConfig{", "ProducerMessage{", "SendMessageInput{", "ch.PublishWithContext(", "rabbitTemplate.send(",
    "@RabbitListener(", "@SqsListener(", "@JmsListener(", "sqsTemplate.send(", "SendMessageRequest.builder().",
    "PublishRequest.builder()", "jmsTemplate.send(", "{", "(", "[", "'", '"', "`", "a", " ", "\t",
    "topic:", "QueueUrl:", "queueUrl(", "->", "to.queue(",
]


@pytest.mark.parametrize("rule", msg.RULES, ids=lambda r: r.id)
def test_every_rule_is_linear_on_adversarial_lines(rule) -> None:
    lines = []
    for token in ADVERSARIAL_TOKENS:
        lines.append(token * (50_000 // len(token)))
    lines.append("".join(ADVERSARIAL_TOKENS) * (50_000 // sum(len(t) for t in ADVERSARIAL_TOKENS)))
    started = time.perf_counter()
    for line in lines:
        for _ in rule.pattern.finditer(line[:50_000]):
            pass
    assert time.perf_counter() - started < 1.0


def test_find_sites_is_linear_on_adversarial_files() -> None:
    started = time.perf_counter()
    for suffix in (".ts", ".py", ".go", ".java"):
        for token in ADVERSARIAL_TOKENS:
            one_line = "kafka amqp sqs " + token * (50_000 // len(token))
            many_lines = "\n".join([token * 10] * 600) + "\nkafka amqp"
            for text in (one_line, many_lines):
                tick = time.perf_counter()
                msg.find_sites(suffix, text)
                assert time.perf_counter() - tick < 1.0, (suffix, token)
        msg.extract_constants("const a = {\n" * 20_000 + "x", suffix)
        msg.extract_constants("class A:\n    x = 'a'\n" * 5_000 + "const (\n" * 5_000, suffix)
    assert time.perf_counter() - started < 10.0
    started = time.perf_counter()
    msg.find_sites(".ts", "kafka producer.send({ topic: " + "'" * 100_000)
    assert time.perf_counter() - started < 1.0


def test_env_and_call_parsers_are_linear() -> None:
    started = time.perf_counter()
    for text in ("process.env." + "a" * 50_000, "os.getenv(" + "'," * 25_000, "os.environ.get('" + "x" * 50_000,
                 "process.env.X ??" + " " * 50_000):
        assert msg.parse_env(text) is None
    msg.call_args("(" * 50_000)
    msg.call_args("a," * 50_000)
    msg.unquote("'" + "\\'" * 25_000)
    assert time.perf_counter() - started < 1.0


# -- F: per-module gating, broker name, synthetic modules, shared predicates ----------------

NODE_MODULE = {
    "package.json": json.dumps({"name": "orders-svc", "dependencies": {
        "kafkajs": "^2", "amqplib": "^0.10"}}),
    "src/client.ts": (
        "import { Kafka } from 'kafkajs'\nimport amqp from 'amqplib'\n"
        "export const kafka = new Kafka({ brokers: [] })\n"),
    "src/orders.ts": (  # never imports kafkajs / amqplib: the clients live in client.ts
        "const ORDERS_TOPIC = 'orders.v1'\n"
        "export class Orders {\n"
        "  async emit(messages) { await this.producer.send({ topic: ORDERS_TOPIC, messages }) }\n"
        "  async listen() { await consumer.subscribe({ topics: [\"payments.v1\"] }) }\n"
        "  bill(buf) { channel.sendToQueue(Queues.BILLING, buf) }\n}\n"
        "const Queues = { BILLING: 'billing' }\n"),
}


@pytest.mark.parametrize("manifest_only", [True, False], ids=["package-json", "source-mention"])
def test_node_calls_without_import_use_module_context(tmp_path: Path, manifest_only: bool) -> None:
    files = dict(NODE_MODULE)
    if not manifest_only:  # no manifest dependency: another source file of the module names it
        files["package.json"] = '{"name": "orders-svc"}'
    else:  # the other file does not mention the brokers either
        files["src/client.ts"] = "export const answer = 42\n"
    manifest = scan_repository(_write(tmp_path / "repo", files)).manifest
    assert _flows(manifest) == {
        ("PRODUCES", "KafkaTopic:orders.v1"), ("CONSUMES", "KafkaTopic:payments.v1"),
        ("PRODUCES", "Queue:rabbitmq:billing")}


def test_python_calls_without_import_use_requirements_txt(tmp_path: Path) -> None:
    manifest = scan_repository(_write(tmp_path / "py-svc", {
        "requirements.txt": "# deps\nconfluent-kafka==2.3\npika>=1.3\n",
        "app/jobs.py": (
            "def run(self):\n"
            "    self.producer.produce('py.orders', value=b'x')\n"
            "    self.channel.basic_publish(exchange='py.ex', routing_key='k', body=b'x')\n"),
    })).manifest
    assert _flows(manifest) == {("PRODUCES", "KafkaTopic:py.orders"),
                                   ("PRODUCES", "Exchange:rabbitmq:py.ex")}
    assert _keys(manifest, "Module") == {"(root)"}


def test_go_calls_without_import_use_go_mod(tmp_path: Path) -> None:
    manifest = scan_repository(_write(tmp_path / "go-svc", {
        "go.mod": ("module example.com/go-svc\n\nrequire (\n"
                   "\tgithub.com/segmentio/kafka-go v0.4.47\n)\n"),
        "internal/w.go": ('package internal\n\nvar w = Writer{Topic: "go.orders"}\n'
                          'func f() { _ = ProducerMessage{Topic: "go.sarama"} }\n'
                          'var r = kafka.ReaderConfig{Topic: "go.read"}\n'),
    })).manifest
    assert {("PRODUCES", "KafkaTopic:go.sarama"), ("CONSUMES", "KafkaTopic:go.read")} <= _flows(manifest)


def test_module_without_the_dependency_ignores_generic_calls(tmp_path: Path) -> None:
    manifest = scan_repository(_write(tmp_path / "repo", {
        "kafka-mod/package.json": '{"name": "k", "dependencies": {"kafkajs": "1"}}',
        "kafka-mod/src/a.ts": "producer.send({ topic: 'real.topic', messages })\n",
        "other/package.json": '{"name": "other"}',
        "other/src/b.ts": ("bus.producer.send({ topic: 'fake.topic', messages })\n"
                           "channel.sendToQueue('fake.q', b)\nconsumer.subscribe({ topic: 'x' })\n"),
    })).manifest
    assert _flows(manifest) == {("PRODUCES", "KafkaTopic:real.topic")}


def test_all_amqp_clients_share_the_rabbitmq_broker(tmp_path: Path) -> None:
    manifest = scan_repository(_write(tmp_path / "repo", {
        "node/package.json": '{"name": "n", "dependencies": {"amqplib": "1"}}',
        "node/a.ts": "ch.sendToQueue('shared', b)\n",
        "jvm/build.gradle": "plugins { id 'java' }\n",
        "jvm/src/A.java": ('class A { void f() { rabbitTemplate.convertAndSend("shared", msg); } }\n'),
    })).manifest
    queues = {e.key for e in manifest.entities if e.type == "Queue"}
    assert queues == {"rabbitmq:shared"}


def test_python_only_repo_gets_a_synthetic_root_module(tmp_path: Path) -> None:
    result = scan_repository(_write(tmp_path / "scripts-repo", {
        "worker.py": "import kafka\nproducer.send('py.t', b'x')\n"}))
    assert _keys(result.manifest, "Module") == {"(root)"}
    assert _entity(result.manifest, "Module", "(root)").attributes["name"] == "scripts-repo"
    assert ("PRODUCES", "KafkaTopic:py.t") in _flows(result.manifest)
    assert result.summary["messaging"]["kafka"]["skipped_no_owner"] == 0


def test_go_only_repo_without_go_mod_gets_a_synthetic_root_module(tmp_path: Path) -> None:
    result = scan_repository(_write(tmp_path / "go-bare", {
        "main.go": 'package main\n\nvar w = kafka.Writer{Topic: "go.t"}\n'}))
    assert _keys(result.manifest, "Module") == {"(root)"}
    assert ("PRODUCES", "KafkaTopic:go.t") in _flows(result.manifest)


def test_requirements_txt_alone_forms_a_module(tmp_path: Path) -> None:
    manifest = scan_repository(_write(tmp_path / "repo", {
        "svc/requirements.txt": "boto3\n-r other.txt\n",
        "svc/app.py": "sqs.send_message(QueueUrl='https://sqs.x.amazonaws.com/1/jobs', Body='x')\n",
        "root.txt": "x",
    })).manifest
    assert _keys(manifest, "Module") == {"svc"}
    assert ("PRODUCES", "Queue:sqs:jobs") in _flows(manifest)


def test_unresolved_and_pattern_predicates() -> None:
    assert msg.is_unresolved_key("unresolved:x") and msg.is_unresolved_key("rabbitmq:unresolved:q")
    assert msg.is_pattern_key("pattern:orders.*") and msg.is_pattern_key("kafka:pattern:a")
    assert not msg.is_unresolved_key("rabbitmq:my.unresolved:x") and not msg.is_pattern_key("sqs:jobs")


def test_scan_defaults_to_one_shared_schema_version(tmp_path: Path, monkeypatch) -> None:
    from ncp.ontology import scan as scan_module

    assert scan_module.DEFAULT_SCAN_SCHEMA_VERSION == 3
    seen: list[int] = []
    real = scan_module.scan_repository

    def spy(root, **kwargs):
        seen.append(kwargs.get("schema_version"))
        return real(root, **kwargs)

    monkeypatch.setattr(scan_module, "scan_repository", spy)
    root = _write(tmp_path / "repo", {"package.json": '{"name": "a"}', "a.ts": "x\n"})
    expected = tmp_path / "expected.json"
    expected.write_text(json.dumps({"entities": [], "relations": []}))
    runner = CliRunner()
    assert runner.invoke(main, ["ontology", "eval", str(expected), "--scan", str(root)]).exit_code == 0
    assert runner.invoke(main, ["ontology", "scan", str(root)]).exit_code == 0
    assert seen == [3, 3]
    assert runner.invoke(main, ["ontology", "eval", str(expected), "--scan", str(root),
                                "--schema-version", "2"]).exit_code == 0
    assert seen[-1] == 2
