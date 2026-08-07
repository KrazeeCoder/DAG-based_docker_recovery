import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "benchmarks"))

from agy_agent import (
    extract_commands,
    extract_usage,
    is_mutating_command,
    parse_final_report,
    parse_stream_events,
)


STREAM = """
{"event":"init","init":{"model":"gemini-3.6-flash-low"}}
{"event":"step_update","step_update":{"state":"DONE","step_type":"tool","tool_name":"run_command","tool_info":{"parameters":{"CommandLine":"docker compose ps"}}}}
{"event":"step_update","step_update":{"state":"DONE","step_type":"tool","tool_name":"run_command","tool_info":{"parameters":{"CommandLine":"docker compose -f x.yaml start database"}}}}
{"event":"result","result":{"status":"SUCCESS","response":"done\\n{\\"status\\":\\"RESTORED\\",\\"diagnosis\\":\\"TARGET_UNAVAILABLE\\",\\"notes\\":\\"started db\\"}\\n","duration_seconds":12.5,"num_turns":2,"usage":{"input_tokens":100,"output_tokens":20,"thinking_tokens":0,"cache_read_tokens":5,"total_tokens":120}}}
"""


class AgyParseTests(unittest.TestCase):
    def test_parse_stream_usage_and_mutations(self):
        events = parse_stream_events(STREAM)
        usage, result = extract_usage(events)
        self.assertEqual(usage["total_tokens"], 120)
        self.assertEqual(result["status"], "SUCCESS")
        commands = extract_commands(events)
        self.assertEqual(len(commands), 2)
        self.assertFalse(is_mutating_command(commands[0]))
        self.assertTrue(is_mutating_command(commands[1]))

    def test_parse_final_report(self):
        report = parse_final_report(
            'Finished.\n{"status":"ABSTAINED","diagnosis":"APPLICATION_OR_UNKNOWN","notes":"app"}'
        )
        self.assertEqual(report["status"], "ABSTAINED")
        self.assertEqual(report["diagnosis"], "APPLICATION_OR_UNKNOWN")


if __name__ == "__main__":
    unittest.main()
