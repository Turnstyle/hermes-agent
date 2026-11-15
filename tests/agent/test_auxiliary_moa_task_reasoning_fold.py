"""The generic auxiliary ``reasoning_effort`` fold skips the MoA tasks: agent/moa_loop.py owns them."""
import logging
from unittest.mock import patch

import pytest

from tests._fixtures.update_recovery_metadata import isolated_update_recovery_metadata

pytestmark = pytest.mark.usefixtures("isolated_update_recovery_metadata")


class TestAuxiliaryTaskExtraBody:
    @pytest.mark.parametrize("moa_task", ["moa_reference", "moa_aggregator"])
    def test_moa_tasks_leave_task_level_reasoning_effort_to_moa(self, moa_task, caplog):
        """MoA resolves ``auxiliary.moa_*.reasoning_effort`` itself, under the preset slot's own
        effort (agent/moa_loop.py), so the generic fold into ``extra_body.reasoning`` skips it:
        folded, it would sit beside (and contradict) a slot's ``reasoning_config``."""
        from agent.auxiliary_client import _get_task_extra_body

        config = {"auxiliary": {moa_task: {"reasoning_effort": "xhigh"}}}
        with patch("hermes_cli.config.load_config", return_value=config), patch("hermes_cli.config.load_config_readonly", return_value=config), \
             caplog.at_level(logging.WARNING, logger="agent.auxiliary_client"):
            result = _get_task_extra_body(moa_task)

        assert "reasoning" not in result
        assert not any(rec.levelno == logging.WARNING for rec in caplog.records)
