import pytest
import asyncio
from unittest.mock import AsyncMock

from src.ingestion.conflict_bg import _run_conflict_detection_bg

@pytest.mark.asyncio
async def test_conflict_detection_failure_updates_job(monkeypatch):
    # Mock conflict_detection.detect_conflicts to raise an exception
    from src.graph import conflict_detection
    
    async def mock_detect(*args, **kwargs):
        raise ValueError("Simulated timeout/failure")
        
    monkeypatch.setattr(conflict_detection, "detect_conflicts", mock_detect)
    
    # Mock data gateway
    from src.data_gateway import get_gateway
    mock_gateway = AsyncMock()
    
    async def mock_get_gateway():
        return mock_gateway
        
    monkeypatch.setattr("src.ingestion.conflict_bg.get_gateway", mock_get_gateway)
    
    # Run the background task directly
    await _run_conflict_detection_bg("CASE-1", "DOC-1")
    
    # Verify update_ingestion_job_by_doc was called with failure status
    mock_gateway.update_ingestion_job_by_doc.assert_called_once()
    args, kwargs = mock_gateway.update_ingestion_job_by_doc.call_args
    assert args[0] == "DOC-1"
    assert args[1]["status"] == "failed"
    assert "Simulated timeout/failure" in args[1]["error_message"]
