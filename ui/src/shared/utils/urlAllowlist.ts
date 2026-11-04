/**
 * Allowlist checks for links the UI builds from model or provider output. Such a link opens only when it is
 * plain https on an expected host: no other scheme (javascript:, data:), no embedded credentials, no port.
 */

/** Return the URL when it is https on a host the predicate accepts, with no credentials or port; otherwise undefined. */
export function allowedHttpsUrl(value: unknown, isAllowedHost: (hostname: string) => boolean): string | undefined {
  if (typeof value !== "string" || !value) return undefined;
  try {
    const url = new URL(value);
    return url.protocol === "https:" && !url.username && !url.password && !url.port && isAllowedHost(url.hostname)
      ? url.href : undefined;
  } catch { return undefined; }
}

// The hosts boto3 signs presigned S3 URLs for: bucket.s3.<region>.amazonaws.com, bucket.s3.amazonaws.com
// (us-east-1), and path-style s3[.<region>].amazonaws.com for bucket names that contain dots. Every quantifier
// is followed by a distinct literal, so a long hostname cannot cause catastrophic backtracking.
const S3_HOST = /^(?:[a-z0-9.-]+\.)?s3(?:\.[a-z]{2}(?:-[a-z]+)+-[0-9]+)?\.amazonaws\.com$/;

/** True when a hostname is an Amazon S3 endpoint, where generated reports are served through presigned URLs. */
export function isAmazonS3Host(hostname: string): boolean {
  return S3_HOST.test(hostname);
}
