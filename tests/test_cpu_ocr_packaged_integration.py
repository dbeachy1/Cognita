"""Real packaged CPU OCR, PostgreSQL cache, and searchable publication.

Also executable inside the app image with stdlib unittest; requires an isolated
COGNITA_TEST_PG_DSN and the read-only model cache, never a live database.
"""
import hashlib
import importlib.util
import json
import os
import re
import tempfile
import unittest
import uuid
from pathlib import Path

from cognita.assets.repository import AssetRepository
from cognita.assets.service import AssetService
from cognita.config import CognitaConfig
from cognita.embeddings import Embedder
from cognita.registry import Project
from cognita.selftest_fixtures import load_manifest, provision_self_test
from cognita.store import Store
from cognita.result_contracts import build_tool_result, validate_structured_payload


@unittest.skipUnless(os.environ.get("COGNITA_TEST_PG_DSN") and
                     Path("/opt/cognita-runtimes/ocr/bin/python").is_file(),
                     "requires isolated PostgreSQL and packaged CPU OCR runtime")
class PackagedOcrPublication(unittest.IsolatedAsyncioTestCase):
    async def test_real_inference_cache_and_search(self):
        temporary = tempfile.TemporaryDirectory(prefix="cognita-ocr-publication-")
        root = Path(temporary.name)
        def cleanup():
            temporary.cleanup()
            self.assertFalse(root.exists(), "owned OCR fixture root remains")
        self.addCleanup(cleanup)
        # 14.2.0: the image holds only the manifest; the weights are in the model cache the throwaway
        # stack mounts here (release.py fetches them there before the stack starts).
        model_cache = Path(os.environ.get("COGNITA_TEST_MODEL_CACHE", "/var/lib/cognita/models"))
        config = CognitaConfig(ocr_python="/opt/cognita-runtimes/ocr/bin/python",
            ocr_model_dir=str(model_cache / "easyocr"),
            ocr_qualification_manifest=Path("/opt/cognita-models/easyocr-qualification.json"),
            ocr_device="cpu", ocr_require_noatime=True, ocr_cpu_threads=4)
        project = Project(name="OcrPkg" + uuid.uuid4().hex[:12], documents_dir=root / "docs", data_dir=root / "data")
        provision_self_test(documents_dir=project.documents_dir, data_dir=project.data_dir,
                            registry_path=root / "registry.yaml")
        store = Store(os.environ["COGNITA_TEST_PG_DSN"], embedding_dimensions=config.embedding_dimensions)
        await store.connect()
        self.addAsyncCleanup(store.close)
        await store.ensure_project(project.name)
        self.addAsyncCleanup(store.drop_project, project.name)
        repository = AssetRepository(store.pool, project.name, dimensions=config.embedding_dimensions)
        embedder = Embedder(config.embedding_model, config.embedding_dimensions,
                            model_cache, threads=4)
        service = AssetService(project, repository, limits=config, embedder=embedder)
        fixture = next(row for row in load_manifest() if row.path.endswith("canonical-clear.png"))
        args = {"filepath": fixture.path, "languages": ["en"]}
        runner_path = Path(os.environ.get("COGNITA_SELFTEST_RUNNER",
                           str(Path(__file__).resolve().parents[1] / "scripts/run-selftest.py")))
        spec = importlib.util.spec_from_file_location("packaged_ocr_runner", runner_path)
        runner = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(runner)
        receipts = runner._ocr_fixture_receipts(project.documents_dir, load_manifest())
        first = await service.ocr_asset(args)
        second = await service.ocr_asset(args)
        query = runner._ocr_search_token(first["text"])
        found = await service.search_assets({"query": query, "path_prefix": fixture.path,
                                             "hybrid_alpha": 0, "max_results": 10})
        # Exercise the wire boundary as well as SQL/service results. An
        # uncataloged full projection previously passed these direct checks
        # but was replaced with internal_error by the real MCP serializer.
        for alpha in (0, 0.3, 1):
            for detail in ("full", "summary"):
                projected = await service.search_assets({"query": query, "path_prefix": fixture.path,
                                                         "hybrid_alpha": alpha, "detail": detail})
                validate_structured_payload("search_assets", projected)
                encoded = build_tool_result("search_assets", projected)
                self.assertFalse(encoded["isError"])
                self.assertEqual(encoded["structuredContent"], projected)
                self.assertTrue(any(row.get("source_sha256") == fixture.sha256
                                    for row in projected["results"]))
        changed = runner._ocr_cache_changes(first, second)
        engine_changed = [key for key in first["engine"].keys() | second["engine"].keys()
                          if (key in first["engine"]) != (key in second["engine"])
                          or first["engine"].get(key) != second["engine"].get(key)]
        counts = {table: await store.pool.fetchval(f'SELECT count(*) FROM {repository.schema}.{table}')
                  for table in ("assets", "asset_ocr_results", "asset_ocr_sources", "asset_ocr_chunks")}
        lexical = await store.pool.fetchval(f"SELECT count(*) FROM {repository.schema}.asset_ocr_chunks WHERE tsv @@ websearch_to_tsquery('english', $1)", query)
        token_counts = [await store.pool.fetchval(f"SELECT count(*) FROM {repository.schema}.asset_ocr_chunks WHERE tsv @@ websearch_to_tsquery('english', $1)", token)
                        for token in re.findall(r"[A-Za-z0-9]{4,}", first["text"])[:24]]
        first_word_nodes = await store.pool.fetchval("SELECT numnode(websearch_to_tsquery('english',$1))", re.findall(r"[A-Za-z0-9]{4,}", first["text"])[0])
        dense = await service.search_assets({"query": query, "path_prefix": fixture.path, "hybrid_alpha": 1})
        print("ocr_diagnostics=" + json.dumps({"cache_hit": second["cache_hit"], "changed_fields": changed,
              "engine_changed_fields": engine_changed, "rows": counts, "lexical_chunk_matches": lexical,
              "search_hits": len(found["results"]), "token_lexical_counts": token_counts, "dense_hits": len(dense["results"]), "first_word_query_nodes": first_word_nodes, "query_sha256": hashlib.sha256(query.encode()).hexdigest()}, sort_keys=True))
        self.assertEqual(receipts, runner._ocr_fixture_receipts(project.documents_dir, load_manifest()))
        with self.subTest(phase="cache"):
            self.assertTrue(second["cache_hit"])
            self.assertEqual(changed, [])
        with self.subTest(phase="search"):
            self.assertTrue(any(row.get("filepath") == fixture.path and row.get("provenance") == "ocr"
                                and row.get("source_sha256") == fixture.sha256 for row in found["results"]))
            self.assertTrue(any(row.get("filepath") == fixture.path for row in dense["results"]))
            self.assertGreater(lexical, 0)
        # Recreating the service exercises persisted cache facts, not an
        # in-process result. Physical binding diagnostics need not persist.
        restarted = AssetService(project, repository, limits=config, embedder=embedder)
        third = await restarted.ocr_asset(args)
        self.assertTrue(third["cache_hit"])
        self.assertEqual(runner._ocr_cache_changes(first, third), [])
        # Replacement preserving size/mtime must not expose the previous OCR.
        source = project.documents_dir / fixture.path
        before = source.stat()
        content = bytearray(source.read_bytes())
        content[-1] ^= 1
        source.write_bytes(content)
        os.utime(source, ns=(before.st_atime_ns, before.st_mtime_ns))
        stale = await service.search_assets({"query": query, "hybrid_alpha": 0})
        self.assertEqual(stale["results"], [])
        self.assertEqual(stale["warnings"][0]["code"], "ocr_freshness")


if __name__ == "__main__":
    unittest.main(verbosity=2)
