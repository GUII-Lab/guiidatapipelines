from django.apps import apps
from django.test import SimpleTestCase

from leai.importing.source_registry import SOURCE_MODEL_DISPOSITIONS


class SourceRegistryTests(SimpleTestCase):
    def test_every_concrete_legacy_model_has_one_valid_disposition(self):
        models = {
            model.__name__
            for model in apps.get_app_config("datapipeline").get_models()
            if not model._meta.abstract and not model._meta.proxy
        }
        self.assertEqual(set(SOURCE_MODEL_DISPOSITIONS), models)
        self.assertEqual(len(SOURCE_MODEL_DISPOSITIONS), len(models))
        for name, entry in SOURCE_MODEL_DISPOSITIONS.items():
            with self.subTest(model=name):
                self.assertIn(entry["disposition"], {"transform", "regenerate", "quarantine", "exclude"})
                self.assertTrue(entry["target"].strip())
                self.assertTrue(entry["reason"].strip())
