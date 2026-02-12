import os
import logging
import tempfile
from core.logging_utils import TimestampedRotatingFileHandler


def test_timestamped_rotation_lines():
    with tempfile.TemporaryDirectory() as tmp_dir:
        log_file = os.path.join(tmp_dir, "test_lines.log")

        # Max lines = 10
        handler = TimestampedRotatingFileHandler(log_file, maxLines=10, backupCount=5)
        formatter = logging.Formatter("%(message)s")
        handler.setFormatter(formatter)

        logger = logging.getLogger("test_logger_lines")
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False

        # Write 5 lines
        for i in range(5):
            logger.info(f"Line {i}")

        assert os.path.exists(log_file)

        # Write another 6 lines -> Total 11 -> Should trigger rollover
        for i in range(5, 11):
            logger.info(f"Line {i}")

        # Verify rotation
        files = os.listdir(tmp_dir)
        timestamped_files = [
            f
            for f in files
            if "test_lines." in f and ".log" in f and f != "test_lines.log"
        ]
        assert len(timestamped_files) >= 1

        handler.close()
