"""Firestore schema validation and data hygiene utilities."""

import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from google.cloud import firestore

logger = logging.getLogger(__name__)


class FirestoreSchemaValidator:
    """Validate Firestore documents against expected schemas."""

    # Expected field types per collection
    SCHEMAS = {
        "incidents": {
            "required": ["problem_id", "status", "severity", "title", "service", "started_at"],
            "types": {
                "problem_id": str,
                "status": str,
                "severity": str,
                "title": str,
                "service": str,
                "started_at": (datetime, str),
                "trace": list,
                "plan": dict,
                "postmortem": str,
            },
            "valid_statuses": [
                "DETECTING",
                "DIAGNOSING",
                "AWAITING_APPROVAL",
                "REMEDIATING",
                "RESOLVED",
                "REJECTED",
                "PREDICTIVE",
            ],
        },
        "predictions": {
            "required": [
                "prediction_id",
                "service",
                "created_at",
                "expires_at",
                "predicted_breach_in_minutes",
                "confidence",
            ],
            "types": {
                "prediction_id": str,
                "service": str,
                "created_at": (datetime, str),
                "expires_at": (datetime, str),
                "predicted_breach_in_minutes": int,
                "confidence": (int, float),
            },
        },
        "incident_clusters": {
            "required": ["cluster_id", "member_incident_ids", "root_cause_summary", "confidence", "status"],
            "types": {
                "cluster_id": str,
                "member_incident_ids": list,
                "root_cause_summary": str,
                "confidence": (int, float),
                "status": str,
            },
        },
        "audit_events": {
            "required": ["actor", "action_type", "incident_id", "seq", "timestamp"],
            "types": {
                "actor": str,
                "action_type": str,
                "incident_id": str,
                "seq": int,
                "timestamp": (datetime, str),
                "details": dict,
            },
        },
    }

    @staticmethod
    def validate_document(collection: str, doc_id: str, data: Dict[str, Any]) -> List[str]:
        """
        Validate a document against the schema.

        Args:
            collection: Collection name
            doc_id: Document ID
            data: Document data

        Returns:
            List of validation errors (empty if valid)
        """
        errors = []
        schema = FirestoreSchemaValidator.SCHEMAS.get(collection)

        if not schema:
            return errors  # Unknown collection; skip validation

        # Check required fields
        for field in schema.get("required", []):
            if field not in data:
                errors.append(f"{collection}/{doc_id}: missing required field '{field}'")

        # Check field types
        for field, expected_type in schema.get("types", {}).items():
            if field in data:
                value = data[field]
                if not isinstance(value, expected_type):
                    errors.append(
                        f"{collection}/{doc_id}: field '{field}' has type {type(value).__name__}, "
                        f"expected {expected_type}"
                    )

        # Validate enum values
        if collection == "incidents" and "status" in data:
            if data["status"] not in schema.get("valid_statuses", []):
                errors.append(
                    f"{collection}/{doc_id}: invalid status '{data['status']}'. "
                    f"Valid values: {schema['valid_statuses']}"
                )

        return errors


class FirestoreRetentionManager:
    """Manage data retention and cleanup policies."""

    def __init__(self, db: firestore.Client):
        """Initialize with Firestore client."""
        self.db = db

    async def cleanup_expired_predictions(self, dry_run: bool = False) -> int:
        """
        Delete predictions that have expired.

        Args:
            dry_run: If True, count documents but don't delete

        Returns:
            Number of documents deleted or that would be deleted
        """
        now = datetime.utcnow()
        deleted_count = 0

        try:
            # Query predictions past their expiration time
            query = self.db.collection("predictions").where("expires_at", "<", now)
            docs = query.stream()

            for doc in docs:
                if not dry_run:
                    doc.reference.delete()
                deleted_count += 1

            if deleted_count > 0:
                action = "Would delete" if dry_run else "Deleted"
                logger.info(f"{action} {deleted_count} expired predictions")

            return deleted_count
        except Exception as e:
            logger.error(f"Error cleaning up expired predictions: {e}")
            return 0

    async def cleanup_resolved_incidents(self, days: int = 90, dry_run: bool = False) -> int:
        """
        Delete RESOLVED incidents older than N days.

        Args:
            days: Age threshold (delete incidents resolved > N days ago)
            dry_run: If True, count documents but don't delete

        Returns:
            Number of documents deleted or that would be deleted
        """
        cutoff_date = datetime.utcnow() - timedelta(days=days)
        deleted_count = 0

        try:
            # Query resolved incidents older than cutoff
            query = (
                self.db.collection("incidents")
                .where("status", "==", "RESOLVED")
                .where("updated_at", "<", cutoff_date)
            )
            docs = query.stream()

            for doc in docs:
                if not dry_run:
                    doc.reference.delete()
                deleted_count += 1

            if deleted_count > 0:
                action = "Would delete" if dry_run else "Deleted"
                logger.info(f"{action} {deleted_count} resolved incidents older than {days} days")

            return deleted_count
        except Exception as e:
            logger.error(f"Error cleaning up old incidents: {e}")
            return 0

    async def cleanup_stale_clusters(self, days: int = 30, dry_run: bool = False) -> int:
        """
        Delete completed or failed clusters older than N days.

        Args:
            days: Age threshold
            dry_run: If True, count documents but don't delete

        Returns:
            Number of documents deleted
        """
        cutoff_date = datetime.utcnow() - timedelta(days=days)
        deleted_count = 0

        try:
            query = (
                self.db.collection("incident_clusters")
                .where("updated_at", "<", cutoff_date)
                .where(
                    "status",
                    "in",
                    ["COMPLETE", "FAILED", "PARTIAL"],
                )
            )
            docs = query.stream()

            for doc in docs:
                if not dry_run:
                    doc.reference.delete()
                deleted_count += 1

            if deleted_count > 0:
                action = "Would delete" if dry_run else "Deleted"
                logger.info(f"{action} {deleted_count} old incident clusters")

            return deleted_count
        except Exception as e:
            logger.error(f"Error cleaning up old clusters: {e}")
            return 0

    def get_collection_stats(self, collection: str) -> Dict[str, Any]:
        """
        Get basic statistics about a collection.

        Args:
            collection: Collection name

        Returns:
            Dictionary with document count, estimated size, etc.
        """
        try:
            docs = self.db.collection(collection).stream()
            doc_list = list(docs)
            doc_count = len(doc_list)

            # Rough size estimate (Firestore counts every document as >= 1KB)
            estimated_size_kb = doc_count  # Conservative estimate

            return {
                "collection": collection,
                "document_count": doc_count,
                "estimated_size_kb": estimated_size_kb,
            }
        except Exception as e:
            logger.error(f"Error getting stats for {collection}: {e}")
            return {"collection": collection, "error": str(e)}
