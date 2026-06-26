import { Storage } from "@google-cloud/storage";

// Google Cloud Storage for scanned invoice images, mapped per push_queue row.
// On Cloud Run the client uses Application Default Credentials (the service's
// compute service account), so no key file is needed — just grant that SA
// object access on the bucket. The bucket name comes from INVOICE_BUCKET; when
// it is unset, image storage is treated as disabled and the queue still works.
const bucketName = process.env.INVOICE_BUCKET?.trim() || "";
const storage = new Storage();

export const invoiceStorageEnabled = Boolean(bucketName);

function bucket() {
  if (!bucketName) {
    throw new Error("INVOICE_BUCKET is not configured");
  }
  return storage.bucket(bucketName);
}

// Object path for a row's page image: "{rowId}/page-{n}.jpg".
export function invoicePagePath(rowId: string, page: number): string {
  return `${rowId}/page-${page}.jpg`;
}

// Uploads the scanned page JPEGs for a row. Best-effort: individual failures are
// logged, not thrown, so a storage hiccup never blocks the enqueue. Returns the
// number of pages successfully stored.
export async function uploadInvoicePages(rowId: string, pages: Buffer[]): Promise<number> {
  if (!invoiceStorageEnabled || pages.length === 0) return 0;
  const b = bucket();
  const results = await Promise.allSettled(
    pages.map((buf, i) =>
      b.file(invoicePagePath(rowId, i)).save(buf, {
        contentType: "image/jpeg",
        resumable: false,
        metadata: { cacheControl: "private, max-age=86400" },
      }),
    ),
  );
  const ok = results.filter((r) => r.status === "fulfilled").length;
  const failed = results.length - ok;
  if (failed > 0) {
    console.error(`[GCS] ${failed}/${results.length} invoice page uploads failed for ${rowId}`);
  }
  return ok;
}

export async function invoicePageExists(rowId: string, page: number): Promise<boolean> {
  if (!invoiceStorageEnabled) return false;
  const [exists] = await bucket().file(invoicePagePath(rowId, page)).exists();
  return exists;
}

export function invoicePageReadStream(rowId: string, page: number) {
  return bucket().file(invoicePagePath(rowId, page)).createReadStream();
}
