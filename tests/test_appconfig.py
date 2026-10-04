from jira_cli.appconfig import compare, flatten

HOCON = """
include "base.conf"
app {
  name = "billing"   # commentaire
  kafka {
    brokers = ${?KAFKA_BROKERS}
  }
  hosts = [ "a", "b" ]
}
http.port: 8080 // autre commentaire
"""


def test_hocon_is_flattened_to_dotted_keys():
    assert flatten("application.conf", HOCON) == {
        "app.name": "billing",
        "app.kafka.brokers": "${?KAFKA_BROKERS}",
        "app.hosts": "[a, b]",
        "http.port": "8080",
    }


def test_new_topic_and_env_var_are_detected_in_hocon():
    after = HOCON.replace(
        "brokers = ${?KAFKA_BROKERS}",
        'brokers = ${?KAFKA_BROKERS}\n    payments-topic = "payments"\n'
        "    payments-topic = ${?PAYMENTS_TOPIC}",
    )
    diff = compare("application.conf", HOCON, after)

    [change] = diff.changes
    assert (change.key, change.kind, change.is_topic) == (
        "app.kafka.payments-topic",
        "ajoutée",
        True,
    )
    assert diff.new_env == {"PAYMENTS_TOPIC": ""}
    assert diff.dropped_env == []


def test_spring_yaml_default_value_and_dropped_variable():
    before = "spring:\n  datasource:\n    url: ${DB_URL}\n"
    after = "spring:\n  datasource:\n    url: jdbc:x\napp:\n  topic: ${TOPIC:orders}\n"
    diff = compare("src/main/resources/application.yml", before, after)

    assert [(c.key, c.kind) for c in diff.changes] == [
        ("app.topic", "ajoutée"),
        ("spring.datasource.url", "modifiée"),
    ]
    assert diff.new_env == {"TOPIC": "orders"}
    assert diff.dropped_env == ["DB_URL"]


def test_properties_and_missing_file():
    after = "# comment\nserver.port=8080\nkafka.topic = ${KAFKA_TOPIC}\n"
    diff = compare("application.properties", None, after)
    assert {c.key for c in diff.changes} == {"server.port", "kafka.topic"}
    assert diff.new_env == {"KAFKA_TOPIC": ""}


def test_moving_a_variable_to_another_key_is_not_new():
    diff = compare("application.properties", "a=${X}\n", "b=${X}\n")
    assert len(diff.changes) == 2
    assert diff.new_env == {} and diff.dropped_env == []


def test_lowercase_substitution_is_a_config_reference_not_an_env_var():
    diff = compare("application.conf", "", "a = ${app.name}\nb = ${?lower}\n")
    assert diff.new_env == {}
