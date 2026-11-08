import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import ts from "typescript";

const source = await readFile(new URL("../src/batchUpload.ts", import.meta.url), "utf8");
const javascript = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
}).outputText;
const { createBatch } = await import(`data:text/javascript;base64,${Buffer.from(javascript).toString("base64")}`);

const file = new File(["zip"], "lote.zip", { type: "application/zip" });
const json = (data, status = 200) => new Response(JSON.stringify(data), {
  status,
  headers: { "Content-Type": "application/json" },
});

function mockRequests(responses) {
  const calls = [];
  return {
    calls,
    request: async (url, options) => {
      calls.push({ url, options });
      const next = responses.shift();
      if (next instanceof Error) throw next;
      assert.ok(next, `Unexpected request: ${url}`);
      return next;
    },
  };
}

test("shows the package limit and does not upload the ZIP again", async () => {
  const mock = mockRequests([
    json({ uploadUrl: "https://storage.example/upload", gcsPath: "gs://bucket/upload" }),
    new Response(null, { status: 200 }),
    json({ detail: "Máximo 20 paquetes por lote." }, 413),
  ]);
  await assert.rejects(
    createBatch(file, "otros_servicios", "https://api.example", mock.request),
    /Máximo 20 paquetes por lote\. Divide el archivo en varios ZIP/,
  );
  assert.deepEqual(mock.calls.map((call) => call.url), [
    "https://api.example/batch/upload-url",
    "https://storage.example/upload",
    "https://api.example/batch/from-gcs",
  ]);
});

test("preserves validation errors returned after the cloud upload", async () => {
  const mock = mockRequests([
    json({ uploadUrl: "https://storage.example/upload", gcsPath: "gs://bucket/upload" }),
    new Response(null, { status: 200 }),
    json({ detail: "ZIP sin carpetas de paquetes." }, 400),
  ]);
  await assert.rejects(createBatch(file, "cuidador", "https://api.example", mock.request), /ZIP sin carpetas de paquetes/);
  assert.equal(mock.calls.length, 3);
});

test("creates a batch through cloud storage", async () => {
  const mock = mockRequests([
    json({ uploadUrl: "https://storage.example/upload", gcsPath: "gs://bucket/upload" }),
    new Response(null, { status: 200 }),
    json({ batchId: "batch", packages: 2 }),
  ]);
  assert.deepEqual(await createBatch(file, "otros_servicios", "https://api.example", mock.request), { batchId: "batch", packages: 2 });
  assert.deepEqual(JSON.parse(mock.calls[2].options.body), { gcsPath: "gs://bucket/upload", service: "otros_servicios" });
  assert.equal(mock.calls.length, 3);
});

test("uses direct upload when signed upload is unavailable", async () => {
  const mock = mockRequests([json({ detail: "No configurado" }, 400), json({ batchId: "batch", packages: 1 })]);
  assert.deepEqual(await createBatch(file, "cuidador", "https://api.example", mock.request), { batchId: "batch", packages: 1 });
  assert.equal(mock.calls[1].url, "https://api.example/batch");
  assert.equal(mock.calls[1].options.body.get("service"), "cuidador");
});

test("uses direct upload if the signed PUT fails", async () => {
  const mock = mockRequests([
    json({ uploadUrl: "https://storage.example/upload", gcsPath: "gs://bucket/upload" }),
    new Response(null, { status: 403 }),
    json({ batchId: "batch", packages: 1 }),
  ]);
  assert.equal((await createBatch(file, "cuidador", "https://api.example", mock.request)).batchId, "batch");
  assert.equal(mock.calls[2].url, "https://api.example/batch");
});

test("does not retry batch creation after a network failure", async () => {
  const mock = mockRequests([
    json({ uploadUrl: "https://storage.example/upload", gcsPath: "gs://bucket/upload" }),
    new Response(null, { status: 200 }),
    new TypeError("network error"),
  ]);
  await assert.rejects(createBatch(file, "cuidador", "https://api.example", mock.request), TypeError);
  assert.equal(mock.calls.length, 3);
});
