"""Offline dispatch regression: a parsed resume command must reach task.run."""
import contextlib
import io
import unittest
from unittest.mock import patch

import xdev
import xdev_tasks


class TaskCliTests(unittest.TestCase):
    def test_resume_reaches_task_dispatch_and_preserves_original_name(self):
        with patch.object(xdev_tasks, 'run', return_value=124) as dispatch:
            self.assertEqual(xdev.main(['task-resume', 'original-task']), 124)
        dispatch.assert_called_once()
        args = dispatch.call_args.args[0]
        self.assertEqual((args.cmd, args.name), ('task-resume', 'original-task'))

    def test_existing_task_commands_keep_their_dispatch(self):
        for argv in (['task-status', 'original-task'],
                     ['task-submit', '-c', 'Write-Output 1', '--name', 'new-task'],
                     ['task-collect', 'original-task', '--out', './result']):
            with self.subTest(argv=argv), patch.object(xdev_tasks, 'run', return_value=3) as dispatch:
                self.assertEqual(xdev.main(argv), 3)
                dispatch.assert_called_once()
                self.assertEqual(dispatch.call_args.args[0].cmd, argv[0])

    def test_resume_help_does_not_invoke_transport(self):
        with patch.object(xdev_tasks, 'run') as dispatch, contextlib.redirect_stdout(io.StringIO()) as output:
            with self.assertRaises(SystemExit) as raised:
                xdev.main(['task-resume', '--help'])
        self.assertEqual(raised.exception.code, 0)
        self.assertIn('task-resume', output.getvalue())
        dispatch.assert_not_called()


if __name__ == '__main__':
    unittest.main(verbosity=2)
