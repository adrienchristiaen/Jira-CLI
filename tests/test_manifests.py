import pytest

from jira_cli.manifests import edit

HELM = """# values preprod
image:
  repository: registry/app
  tag: "1.0.0"
env:
  FOO: bar
"""

KUSTOMIZE = """resources:
- ../base
images:
- name: app
  newTag: 1.0.0
"""

RAW = """apiVersion: v1
kind: Service
metadata:
  name: app
---
apiVersion: apps/v1
kind: Deployment
spec:
  template:
    spec:
      containers:
        - name: app
          image: registry/app:1.0.0
          env:
            - name: FOO
              value: "bar"
"""


def test_helm_values_keep_comments_and_add_missing_env_only():
    out = edit(HELM, "image.tag", "1.1.0", "env", {"KAFKA_TOPIC": "payments", "FOO": "changed"})
    assert out == HELM.replace('"1.0.0"', '"1.1.0"') + "  KAFKA_TOPIC: payments\n"


def test_kustomize_image_selected_by_name_keeps_list_style():
    out = edit(KUSTOMIZE, "images[name=app].newTag", "1.1.0")
    assert out == KUSTOMIZE.replace("1.0.0", "1.1.0")


def test_raw_multi_document_manifest():
    container = "spec.template.spec.containers[name=app]"
    out = edit(
        RAW, f"{container}.image", "registry/app:1.1.0", f"{container}.env", {"TOPIC": "orders"}
    )
    expected = RAW.replace("app:1.0.0", "app:1.1.0") + (
        "            - name: TOPIC\n              value: orders\n"
    )
    assert out == expected


def test_index_path():
    out = edit(RAW, "spec.template.spec.containers[0].image", "x:2")
    assert "image: x:2" in out


@pytest.mark.parametrize(
    "path", ["image.nope", "images[name=other].newTag", "images[name=app]", "a..b"]
)
def test_unknown_or_invalid_paths_are_refused(path):
    with pytest.raises(ValueError):
        edit(KUSTOMIZE if "images" in path else HELM, path, "1")
