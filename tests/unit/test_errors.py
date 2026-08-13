import pytest

from app.core.errors import EliaraError, LLMUnavailableError, SQLValidationError


def test_public_message_never_leaks_internal_detail():
    err = SQLValidationError(internal_detail="ATTACH detected in: SELECT ...; ATTACH x")
    assert "ATTACH" not in err.public_message
    assert err.status_code == 422


def test_hierarchy():
    assert issubclass(LLMUnavailableError, EliaraError)
    with pytest.raises(EliaraError):
        raise LLMUnavailableError("api down")
