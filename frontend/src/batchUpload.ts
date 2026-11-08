export type BatchCreateData = {
  batchId: string;
  packages: number;
};

async function responseError(response: Response): Promise<string> {
  const text = await response.text();
  let message = text;
  try {
    const body: unknown = JSON.parse(text);
    if (body && typeof body === "object" && "detail" in body) {
      const detail = body.detail;
      if (typeof detail === "string") message = detail;
      else if (detail && typeof detail === "object" && "message" in detail && typeof detail.message === "string") {
        message = detail.message;
      } else message = "";
    } else message = "";
  } catch {
    // Non-JSON responses can still contain a useful error message.
  }
  if (!message.trim()) message = `No se pudo crear el lote (HTTP ${response.status}).`;
  if (response.status === 413 && /paquetes por lote/i.test(message)) {
    message += " Divide el archivo en varios ZIP y vuelve a intentarlo.";
  }
  return message;
}

export async function createBatch(
  file: File,
  service: string,
  apiBase: string,
  request: typeof fetch = fetch,
): Promise<BatchCreateData> {
  let gcsPath: string | null = null;
  try {
    const response = await request(`${apiBase}/batch/upload-url`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ filename: file.name, service }),
    });
    if (response.ok) {
      const data = await response.json();
      if (typeof data?.uploadUrl === "string" && typeof data?.gcsPath === "string") {
        const upload = await request(data.uploadUrl, {
          method: "PUT",
          headers: { "Content-Type": "application/zip" },
          body: file,
        });
        if (upload.ok) gcsPath = data.gcsPath;
      }
    }
  } catch {
    // A failure before creating the batch can fall back to a direct upload.
  }

  if (gcsPath) {
    const response = await request(`${apiBase}/batch/from-gcs`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ gcsPath, service }),
    });
    if (!response.ok) throw new Error(await responseError(response));
    return response.json();
  }

  const form = new FormData();
  form.append("file", file);
  form.append("service", service);
  const response = await request(`${apiBase}/batch`, { method: "POST", body: form });
  if (!response.ok) throw new Error(await responseError(response));
  return response.json();
}
