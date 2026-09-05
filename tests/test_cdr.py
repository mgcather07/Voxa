"""CUCM CDR/CMR parsing — the code most exposed to version drift across
customers' clusters. Fixtures are trimmed real CUCM-15 headers/rows, including
the details that broke a live ingest: the UNIQUEIDENTIFIER type row and MOS
living inside the varVQMetrics blob rather than a top-level column.
"""

from pathlib import Path

from app import cdr


# --- header/type-row recognition -------------------------------------------
class TestTypeRow:
    def test_recognizes_sql_type_row_including_uniqueidentifier(self):
        # The real CUCM-15 type row that crashed ingest before the shape fix.
        cells = ["INTEGER", "INTEGER", "VARCHAR(50)", "UNIQUEIDENTIFIER",
                 "VARCHAR(128)", "INTEGER"]
        assert cdr._is_type_row(cells) is True

    def test_data_row_is_not_a_type_row(self):
        cells = ["1", "16", "SEP001122334455", "10.0.0.5", "7001", "1693900000"]
        assert cdr._is_type_row(cells) is False

    def test_empty_cells_ignored(self):
        assert cdr._is_type_row(["INTEGER", "", "VARCHAR(10)", ""]) is True
        assert cdr._is_type_row(["", "", ""]) is False


class TestIsCmr:
    def test_cmr_detected_by_varvqmetrics(self):
        keys = ["cdrrecordtype", "devicename", "varvqmetrics", "jitter"]
        assert cdr._is_cmr(keys) is True

    def test_cmr_detected_by_packet_counts(self):
        keys = ["devicename", "numberpacketssent", "numberpacketslost"]
        assert cdr._is_cmr(keys) is True

    def test_cdr_is_not_cmr(self):
        keys = ["cdrrecordtype", "origdevicename", "destdevicename",
                "datetimeorigination", "duration"]
        assert cdr._is_cmr(keys) is False


# --- varVQMetrics + MOS extraction -----------------------------------------
class TestVqMetrics:
    def test_parses_semicolon_blob(self):
        row = {"varvqmetrics": "MLQK=4.21;MLQKav=4.03;CS=4;SCS=1;VoRxCodec=G.711 u-la"}
        vq = cdr._vq_metrics(row)
        assert vq["mlqkav"] == "4.03"
        assert vq["cs"] == "4"
        assert vq["vorxcodec"] == "G.711 u-la"

    def test_missing_blob_is_empty(self):
        assert cdr._vq_metrics({}) == {}

    def test_mos_from_mlqkav_when_no_top_level_column(self):
        row = {"varvqmetrics": "MLQKav=4.03;CS=0"}
        assert cdr._mos_from_row(row) == 4.03

    def test_mos_prefers_top_level_column(self):
        row = {"mos": "3.9", "varvqmetrics": "MLQKav=4.5"}
        assert cdr._mos_from_row(row) == 3.9

    def test_leg_without_mlqk_returns_none_not_zero(self):
        # Rows 3-5 of the real sample: codec/CS present, no MLQK -> no MOS.
        row = {"varvqmetrics": "CS=5;SCS=0;VoRxCodec=G.722 64k"}
        assert cdr._mos_from_row(row) is None

    def test_zero_mos_treated_as_missing(self):
        assert cdr._mos_from_row({"varvqmetrics": "MLQKav=0.0"}) is None


# --- end-to-end file ingest via fixtures -----------------------------------
CDR_FIXTURE = (
    '"cdrRecordType","globalCallID_callManagerId","globalCallID_callId",'
    '"origLegCallIdentifier","destLegCallIdentifier","dateTimeOrigination",'
    '"dateTimeConnect","dateTimeDisconnect","duration","callingPartyNumber",'
    '"originalCalledPartyNumber","finalCalledPartyNumber","origDeviceName",'
    '"destDeviceName","origIpAddr","destIpAddr","origCause_value","destCause_value"\n'
    'INTEGER,INTEGER,INTEGER,INTEGER,INTEGER,INTEGER,INTEGER,INTEGER,INTEGER,'
    'VARCHAR(50),VARCHAR(50),VARCHAR(50),VARCHAR(129),VARCHAR(129),'
    'VARCHAR(64),VARCHAR(64),INTEGER,INTEGER\n'
    '1,1,5001,111,222,1693900000,1693900005,1693900020,15,"7001","7002","7002",'
    '"SEP001122334455","SEP00AABBCCDDEE","10.0.0.5","10.0.0.6",0,16\n'
)

CMR_FIXTURE = (
    '"cdrRecordType","globalCallID_callManagerId","globalCallID_callId",'
    '"nodeId","directoryNum","callIdentifier","dateTimeStamp",'
    '"numberPacketsSent","numberPacketsReceived","numberPacketsLost",'
    '"jitter","latency","deviceName","varVQMetrics","duration"\n'
    'INTEGER,INTEGER,INTEGER,INTEGER,VARCHAR(50),INTEGER,INTEGER,'
    'INTEGER,INTEGER,INTEGER,INTEGER,INTEGER,VARCHAR(129),VARCHAR(600),INTEGER\n'
    '2,1,5001,1,"7001",222,1693900005,1000,998,2,4,30,"SEP001122334455",'
    '"MLQK=4.21;MLQKav=4.03;MLQKmn=3.95;CS=4;SCS=1;VoRxCodec=G.711 u-la",15\n'
)


class TestFileIngest:
    def _write(self, tmp_path: Path, name: str, content: str) -> Path:
        p = tmp_path / name
        p.write_text(content)
        return p

    def test_cdr_row_parsed_type_row_skipped(self, tmp_path):
        rows = list(cdr._rows(self._write(tmp_path, "cdr_test", CDR_FIXTURE)))
        assert len(rows) == 1, "type row must be skipped, one data row left"
        rec = cdr._cdr_record(rows[0])
        assert rec is not None
        assert rec.orig_device == "SEP001122334455"
        assert rec.final_called == "7002"
        assert rec.duration == 15
        assert rec.dest_cause == 16

    def test_cmr_row_parsed_with_mos_codec_concealment(self, tmp_path):
        rows = list(cdr._rows(self._write(tmp_path, "cmr_test", CMR_FIXTURE)))
        assert len(rows) == 1
        q = cdr._cmr_quality(rows[0])
        assert q is not None
        assert q.device == "SEP001122334455"
        assert q.mos == 4.03            # from MLQKav in varVQMetrics
        assert q.codec == "G.711 u-la"
        assert q.concealed_secs == 4
        assert q.severely_concealed_secs == 1
        assert q.packets_lost == 2 and q.packets_sent == 1000

    def test_cmr_detected_as_quality_not_cdr(self, tmp_path):
        header = cdr._first_header(self._write(tmp_path, "cmr_test", CMR_FIXTURE))
        assert cdr._is_cmr(header) is True
        header = cdr._first_header(self._write(tmp_path, "cdr_test", CDR_FIXTURE))
        assert cdr._is_cmr(header) is False


# --- retention prune --------------------------------------------------------
class TestPruneProcessed:
    def test_prunes_only_old_files(self, tmp_path):
        import os
        import time

        proc = tmp_path / "processed"
        proc.mkdir()
        old, new = proc / "cdr_old", proc / "cdr_new"
        old.write_text("x")
        new.write_text("y")
        past = time.time() - 100 * 86400
        os.utime(old, (past, past))
        removed = cdr.prune_processed(tmp_path, days=90)
        assert removed == 1
        assert new.exists() and not old.exists()

    def test_days_zero_keeps_everything(self, tmp_path):
        proc = tmp_path / "processed"
        proc.mkdir()
        (proc / "cdr_a").write_text("x")
        assert cdr.prune_processed(tmp_path, days=0) == 0
        assert (proc / "cdr_a").exists()
