"""scripts/cloud_env.py: one env list for local and cloud, with the secrets kept out of it."""
import importlib.util
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("cloud_env", ROOT / "scripts" / "cloud_env.py")
cloud_env = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cloud_env)

CLOUD = {"INV_STUDIO_CLOUD": "1", "GOOGLE_CLOUD_PROJECT": "p", "INV_STUDIO_BUCKET": "b",
         "INV_STUDIO_ALLOWED_EMAILS": "owner@example.test"}


def test_the_env_list_is_read_as_key_values_with_blanks_skipped():
    text = "# comment\nexport A=1\nB='two words'\nC=\n\nD=\"it's\"\n"
    assert cloud_env.parse(text) == {"A": "1", "B": "two words", "D": "it's"}
    with pytest.raises(SystemExit, match="line 1"):
        cloud_env.parse("not a pair")


def test_the_yaml_quotes_every_value_and_fills_the_cloud_data_dir():
    lines = cloud_env.render({**CLOUD, "INV_STUDIO_PORT": "8080", "INV_STUDIO_AZURE_ENDPOINT": "https://r.openai.azure.com",
                              "INV_STUDIO_AI_PRICES": '{"d": {"input": 1}}', "NOTE": "it's"})
    assert lines[0].startswith("#")
    assert "INV_STUDIO_DATA: '/tmp/inv-studio-data'" in lines
    assert "INV_STUDIO_AZURE_ENDPOINT: 'https://r.openai.azure.com'" in lines
    assert "NOTE: 'it''s'" in lines
    assert not any(line.startswith("INV_STUDIO_PORT") for line in lines)


@pytest.mark.parametrize("secret", ["INV_STUDIO_DATABASE_URL", "INV_STUDIO_VAULT_KEY", "INV_STUDIO_AZURE_API_KEY"])
def test_a_secret_manager_credential_is_refused_from_the_env_file(secret):
    with pytest.raises(SystemExit, match="Secret Manager"):
        cloud_env.render({**CLOUD, secret: "value"})


def test_a_cloud_file_must_say_so_and_name_its_project_bucket_and_owner():
    with pytest.raises(SystemExit, match="INV_STUDIO_CLOUD=1"):
        cloud_env.render({"GOOGLE_CLOUD_PROJECT": "p"})
    with pytest.raises(SystemExit, match="INV_STUDIO_BUCKET"):
        cloud_env.render({"INV_STUDIO_CLOUD": "1", "GOOGLE_CLOUD_PROJECT": "p"})


def test_the_example_files_name_the_azure_variables_without_a_key_value():
    example = (ROOT / ".env.example").read_text()
    assert cloud_env.parse(example) == {}, "the example must not carry values"
    for name in ("INV_STUDIO_AZURE_ENDPOINT", "INV_STUDIO_AZURE_API_KEY", "INV_STUDIO_AZURE_DEPLOYMENT",
                 "INV_STUDIO_DEFAULT_PROVIDER", "INV_STUDIO_AI_PRICES", "VERTEX_PROJECT_ID"):
        assert f"{name}=" in example
    yaml = (ROOT / "deploy" / "cloud.env.example.yaml").read_text()
    assert "INV_STUDIO_AZURE_API_KEY:" not in yaml and "INV_STUDIO_AZURE_ENDPOINT" in yaml
