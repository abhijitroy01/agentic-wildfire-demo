"""Bounded Bedrock Retrieve tool for the reviewed public guidance collection; the Runtime has no other."""
import logging
import os
from urllib.parse import urlsplit

from strands import tool
from utils.log_safety import error_summary

logger = logging.getLogger(__name__)
PUBLIC_COLLECTION = "wildfire-public-guidance-v1"
OFFICIAL_HOSTS = {"www.ready.gov", "www.fs.usda.gov", "www.nwcg.gov", "www.nps.gov"}


def unavailable(message):
    return {"success": False, "documents": [], "citations": [], "error": message}


def official_url(value):
    """Only reviewed government sources can become public citation links."""
    if not isinstance(value, str):
        return None
    try:
        parsed = urlsplit(value)
        if (parsed.scheme != "https" or parsed.hostname not in OFFICIAL_HOSTS
                or parsed.username or parsed.password or parsed.port not in (None, 443)):
            return None
    except ValueError:
        return None
    return value


def _retrieve_from_knowledge_base(query, num_results=7, *, kb_id):
    """Retrieve reviewed public passages from one knowledge base, keeping only allowlisted official sources."""
    if not isinstance(query, str) or not query.strip() or len(query) > 6000:
        return unavailable("Provide a nonempty procedure question and context of at most 6,000 characters.")
    config = {"numberOfResults": min(max(num_results, 1), 10),
              "filter": {"equals": {"key": "collection", "value": PUBLIC_COLLECTION}}}
    try:
        from utils.aws_clients import AWSClients
        response = AWSClients.bedrock_agent_runtime().retrieve(
            knowledgeBaseId=kb_id, retrievalQuery={"text": query},
            retrievalConfiguration={"vectorSearchConfiguration": config},
        )
        documents, citations = [], []
        for result in response.get("retrievalResults", []):
            content = result.get("content", {}).get("text", "")
            if not content.strip():
                continue
            metadata = result.get("metadata", {})
            source_url = official_url(metadata.get("source_url"))
            if (metadata.get("collection") != PUBLIC_COLLECTION
                    or metadata.get("classification") != "public" or not source_url):
                continue
            uri = result.get("location", {}).get("s3Location", {}).get("uri", "")
            title = metadata.get("title") or uri.rsplit("/", 1)[-1] or "Untitled source"
            citation = {
                "documentName": title, "excerpt": content[:500], "sourceUrl": source_url,
                **({"publisher": metadata["publisher"]} if metadata.get("publisher") else {}),
                **({"sourceDate": metadata["source_date"]} if metadata.get("source_date") else {}),
                **({"retrievedAt": metadata["retrieved_at"]} if metadata.get("retrieved_at") else {}),
            }
            documents.append({
                "id": len(documents)+1, "content": content, "source": title,
                "source_url": source_url, "relevance_score": result.get("score"),
                "citation": citation,
            })
            if citation not in citations:
                citations.append(citation)
        logger.info("KB retrieval: documents=%s", len(documents))
        if not documents:
            return unavailable("No matching reviewed guidance was retrieved. Procedure guidance is unavailable for this question.")
        return {"success": True, "documents": documents, "citations": citations,
                "guidance_scope": "Published planning guidance; not incident orders or verified safe routes."}
    except Exception as exc:  # noqa: BLE001 — the public tool returns a safe unavailable receipt
        logger.error("Knowledge base retrieval failed: %s", error_summary(exc))
        return unavailable("Procedure retrieval failed. Do not substitute an uncited procedure.")


@tool
def query_public_knowledge_base(query: str, context: str = "") -> dict:
    """Retrieve reviewed official public guidance; never fall back to the private collection."""
    kb_id = os.getenv("PUBLIC_DEMO_KNOWLEDGE_BASE_ID", "").strip()
    if not kb_id:
        return unavailable("Public procedure retrieval is unavailable: no public knowledge base is configured.")
    return _retrieve_from_knowledge_base(
        f"{query}\n\nIncident context (scenario assumptions):\n{context}", kb_id=kb_id,
    )
