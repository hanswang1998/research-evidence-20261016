"""Small, offline-first control layer for new research launches.

The existing N14 handoff's `current` section is authoritative. Historical fields
and frozen experiments are never rewritten by this module. This is an accident
and concurrency guard, not a security boundary against a writer who can edit
this module, the handoff, or the evidence. Only `run` can start a child process;
status/check and the unit tests do not contact JoinQuant.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
import functools
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
MASTER = 'analysis/yjj_n14_strict_limit_baseline_20260914/ITERATION_HANDOFF.json'
ACCEPTANCE = 'docs/研究验收标准.md'
SCHEMA = 'research_control.v1'
SCOPED_SCHEMA = 'research_control.v2'
LINES = ('main_strategy', 'rust_replay')
TERMINAL = {'completed', 'failed', 'confirmed_not_submitted'}
GATES = {
    'full_terminal_multiple_strictly_above': '2613.616112556',
    'full_platform_drawdown_max': '29.75',
    'recent_return_min': '47.38',
    'recent_platform_drawdown_max': '15.62',
    'recent_daily_drawdown_max': '15.618324234512215',
    'full_daily_drawdown_max': '29.74955069859122',
}
COMPACT_GATE_VERSION = 'compact_dual_20260920.v1'
COMPACT_FULL_GATE_VERSION = 'compact_full_20260921.v1'
COMPACT_BASELINE = 'analysis/yjj_structure_research_20260920/BASELINE_LOCK.json'
COMPACT_BASELINE_SHA256 = '38b2ae0b28c95d025329aadec6d2ef60b21a34350ef136b3c84c71b31ba9255e'
# Exact approved development prefix. Historical full/dual contracts retain
# their original windows; this does not authorize submission or replace them.
COMPACT_PREFIX_SCOPE = 'analysis/yjj_structure_research_20260920/H409_current_native_adapter_20261004/BASELINE_SCOPE.json'
COMPACT_PREFIX_SCOPE_SHA256 = 'd62a809d67dc1de3ed53b0befec859304d070df45faa2b288cb3f4c9b5a06321'
# First H718 native development stage follows the persistent human original
# 10/10 target and current MS02820. Existing frozen H244-strict contracts stay
# unchanged. This exact policy does not grant a submission or a broader scope.
H718_NATIVE_ADMISSION_POLICY = {
    'path': 'analysis/yjj_structure_research_20260920/H718_native_precommit_20261011/NATIVE_ADMISSION_POLICY.json',
    'sha256': 'ac99c09a99429dd5f2e50aee7fa2b32cab61c81b5e3b4a50a08ff9e3c8c1acf6',
}
H718_NATIVE_ADMISSION_SOURCE = '0fcd90f593fe5b65a158b1b89219d7f45d152e3dafcdf14b17736407db0b42f9'
# The human explicitly allowed one extra compatibility repair and one task.
# Both the new source and exception receipt are pinned; the failed task remains.
H718_DATE_COMPAT_ADMISSION_POLICY = {
    'path': 'analysis/yjj_structure_research_20260920/H718_native_runtime_compatibility_20261011/NATIVE_ADMISSION_POLICY.json',
    'sha256': 'bb3aec404e0010d59eb470165a990c6457f937505d894aaed43006574032b410',
}
H718_DATE_COMPAT_ADMISSION_SOURCE = '58e2bfa8a52c4eb6d63b7379064b7685f41406fac274bc0ce69805d39bbe867f'
# A second explicit exception qualifies only the two reviewed builtins.all calls.
# It consumes one new repair/task and preserves both previous failed tasks.
H718_NAMESPACE_COMPAT_ADMISSION_POLICY = {
    'path': 'analysis/yjj_structure_research_20260920/H718_native_runtime_namespace_20261011/NATIVE_ADMISSION_POLICY.json',
    'sha256': 'c6acbac109a62723ab6aa7895f6cf574f8566b70b50f6fc8b7eb3884ec5bcb7d',
}
H718_NAMESPACE_COMPAT_ADMISSION_SOURCE = '12ffe7452b5dbe88ca9fc4d1452893e9f09f495c136e7a410798f0482311fd24'
# Exactly retained H781, prepared under the ongoing human conditional target.
# This validates policy and read-only inputs; it grants no submission or adoption.
H781_NATIVE_ADMISSION_POLICY = {
    'path': 'analysis/yjj_structure_research_20260920/H781_native_precommit_20261012/NATIVE_ADMISSION_POLICY_R1.json',
    'sha256': '4cd79483a9dd2baf549d343fd2bf405883989a08f501886bd7a4c0f2dbe8481a',
}
H781_NATIVE_ADMISSION_SOURCE = 'f3d9caa2314c70493e00761aa7552b5309501d276605847cbebaaa50718bfd02'
H781_ECONOMIC_SOURCE = {
    'path': 'analysis/yjj_structure_research_20260920/H781_completed_cohort_sequence_drawdown_guard/strategy.py',
    'sha256': '3be0c6a8b471daa656640cf9adc0eb3c37e4a95b98d711f10e1b35a4a717cd57',
}
H781_EXTERNAL_READ_ONLY_BINDING = {
    'path': 'analysis/yjj_structure_research_20260920/H781_native_precommit_20261012/EXTERNAL_READ_ONLY_BINDINGS.json',
    'sha256': 'ffef5b88ea201490af1c66599ec9fce04e73535bd17513535bf1414124576506',
}
# One additional task explicitly authorized for the reviewed observer repair.
# The consumed original grant and its failed task remain separate.
H781_OBSERVER_ADMISSION_POLICY = {
    'path': 'analysis/yjj_structure_research_20260920/H781_native_observer_resubmit_20261015/NATIVE_ADMISSION_POLICY.json',
    'sha256': '43b95221dcd86a531dc71b2f67c017250e78823f9d0c3d23a5803ddb39cc5a33',
}
H781_OBSERVER_ADMISSION_SOURCE = '7c78c2585b49503def4879ff2ef162c4ab9cc7c820addf2e6a6535d06638d14e'
H781_OBSERVER_READ_ONLY_BINDING = {
    'path': 'analysis/yjj_structure_research_20260920/H781_native_observer_resubmit_20261015/EXTERNAL_READ_ONLY_BINDINGS.json',
    'sha256': 'a6a953e24b7e9644257cab2f4204db698fdc6082de3b2756b8471cefd0ebb6a0',
}
SAFE_ENV = {
    'JQ_STRICT_SINGLE_SUBMIT': '1', 'JQ_SUBMIT_RETRY_COUNT': '1',
    'JQ_EXPECT_BASE_CAPITAL': '100000',
    'JQ_ENFORCE_PLATFORM_RUNTIME_BINDING': '1',
    'JQ_PAGE_SIDE_SUBMIT_IDENTITY_CAPTURE': '1',
    'JQ_REQUIRE_EMPTY_SLOT_BEFORE_SUBMIT': '1',
    'JQ_ALLOW_UNIQUE_SLOT_CONTRACT_RECONCILIATION': '1',
    'JQ_ALLOW_RUNTIME_CREDIT_CONFIRM': '0', 'JQ_AUTO_LOG_EXPORT': '0',
    'JQ_BACKTEST_DISABLE_LOCAL_LOCKS': '0', 'JQ_BACKTEST_SKIP_QUEUE': '0',
    'JQ_BACKTEST_PARENT_HOLDS_BUDGET': '0',
}
VALUE_FLAGS = {'--slot', '--output-dir', '--study-label', '--task-name',
               '--goal-packet', '--research-plan'}
BOOLEAN_FLAGS = {'--full', '--tick', '--minute', '--no-diagnosis', '--skip-log', '--no-export',
                 '--skip-attribution', '--new-slot'}
REQUIRED_FLAGS = {'--full', '--no-diagnosis', '--skip-log', '--skip-attribution'}
# A narrowly reviewed data-only exception. This exact source only reads prices,
# emits data and checks an empty account. New collectors require source review;
# tagging an ordinary strategy as data collection cannot enable minute launches.
REVIEWED_MINUTE_COLLECTIONS = {
    '8888c4ad41343a8e675f9d29b924fb3232e09ac7a323af21220b4ebe74570d7c':
        ('2026-08-17', '2026-09-07'),
    '271d2382574e5bea2fd87ab6900f0c09c4c79f819e8610a13808a73d698ba864':
        ('2026-03-09', '2026-04-20'),
    'b865aa6b57fe49e0367a0a4141c68d22925a11c8978102ddcc98ee8eecc64780':
        ('2026-04-21', '2026-06-04'),
    '8517679642d52d1eef5b8d54a47b8a0f6452e6adfaf8116ccae30ba5515b1e2f':
        ('2026-06-05', '2026-07-17'),
    '7773b6747d9bae2e71d45065946f592da747cf6ddbabbd32e52c6fda3d8f9b9d':
        ('2026-07-20', '2026-08-14'),
    'cf71a81d16272bcc28efa4a7da7e71a5605cf569e9e2e684f6623d9d77c6bd4a':
        ('2026-03-20', '2026-03-20'),
    # Three reviewed full-market preopen pilots; exact source/window only.
    '569787f8beadc4e3068f36e2be839730d6398d29b5705fcec0190692e604ee35':
        ('2020-01-06', '2020-01-06'),
    'af042ea5e0bbf45939d1b153fe8fc06842d9267fb07552232a6086a42fe0d0b0':
        ('2023-01-03', '2023-01-03'),
    'cedcf1879a28550bc7d9ab62cd498f3f67fed5c3bfe2ce5afa18779b9caae487':
        ('2026-03-09', '2026-03-09'),
    # Reviewed ten-date native preopen segment; same verified collector template.
    '2f2e54a301dbd978e5881390ed98b0fa0386620088ab31c9e3bdf8ded3197e36':
        ('2020-01-02', '2020-01-16'),
    # Reviewed historical preopen batch sources; exact hashes and windows only.
    '3adfd829eaf88e82a185ebcdb3b4339407e472d46e3e816ddd2dbacd57b6e92d':
        ('2020-01-17', '2020-02-07'),
    '12cf579432c74c8f8e857618b1a4803cb8051dba401a33d9f88384f821d9ee0b':
        ('2020-02-10', '2020-02-21'),
    '69b74340fd29e767ce391377cdd487c182fa12e6e391d713ee4e1bb1fb8f2da5':
        ('2020-02-24', '2020-03-06'),
    '26fde9bdef572af52603571eb459310aaf2b9838721f341d0aaa166301d08305':
        ('2020-03-09', '2020-03-20'),
    '8778cae42011b51d8732ac28e7189e4e99e8397794ddc7d1c3a1607c3f6375fd':
        ('2020-03-23', '2020-04-03'),
    'ecc47a4d5b41014d55aa77944f39cb4ad0ef750987a2bf9e2a078cbacad4a01e':
        ('2020-04-07', '2020-04-20'),
    '1dfc91ebf0d3045761ce1cf2bde0faaaf9a21fff02397c4dd46d721a302cfb95':
        ('2020-04-21', '2020-05-07'),
    'dcc5f494270f139c0f7f2f6ed4696d03d39690727e5709b67f80300f8c8bed5a':
        ('2020-05-08', '2020-05-21'),
    '428acd39aef361bcc04641eca9ada2aedebdd454b1e70cbff613a6482b35239a':
        ('2020-05-22', '2020-06-04'),
    '6632d648670aa705540006835620243edf291cdc7ee70efa463c711c6a4c970c':
        ('2020-06-05', '2020-06-18'),
    '837b92e778e3f7518b3587394bd0a37bacfa9bc42e487d4c874583aef3b306b2':
        ('2020-06-19', '2020-07-06'),
    'b491b198d60d0ec80bf1e7e037343cb85124e73922ef381f2a6430184d4f259e':
        ('2020-07-07', '2020-07-20'),
    'aabaacf5b6a63d3556cf34b860b3c051f9491e4615105acece7aca344fa4aac4':
        ('2020-07-21', '2020-08-03'),
    '6cbd7e457984db61cd323e79e02069b9c7c52e26f3be02acfd08a2850275158b':
        ('2020-08-04', '2020-08-17'),
    '904d5cc4363e92630d8771f53696fd88b3af86bae45b12925a17cbf620eb40ef':
        ('2020-08-18', '2020-08-31'),
    '7c78bab39f569e17b2b2a717d6452c87a16c34416ce26a7455f9b3dc8449ef0e':
        ('2020-09-01', '2020-09-14'),
    'e725328a457c5c0ed98a1c05e6648ae2abe967171145e3e71a669fe43dde7e0b':
        ('2020-09-15', '2020-09-28'),
    '5e36a99c0c72281d97a2378c95722c88f2aa88c6eee3f797ee2b068f850a84eb':
        ('2020-09-29', '2020-10-20'),
    'edca570a8ef6a8b602e4713d2c6913ed121101cb4cd1ebba645dcd67364adc64':
        ('2020-10-21', '2020-11-03'),
    '02ebe3b72ce6567dd05454309ed1d4df18acee9dc4833ef1576e521078521c58':
        ('2020-11-04', '2020-11-17'),
    '4f5661cc5958f9e04461283a5044eb902bdae72a91b04163b377405e4df58f36':
        ('2020-11-18', '2020-12-01'),
    '82079eafded0851d18f03e1c4c68486087600ad985230d76a6b0064a4719dd36':
        ('2020-12-02', '2020-12-15'),
    '69b45159b16b20d9fffcd55ef43890dd2ac7eb934fd2775f368ed28b03edfb39':
        ('2020-12-16', '2020-12-29'),
    'e4b0a93e086d90ba7f2919c301902b2c77c8a99a547c5ffa80c77544385dbf20':
        ('2020-12-30', '2021-01-13'),
    'ff5ed87899b49bcd73f0b55163e5354b72931a11a4978fe468242ae8aebcfa8a':
        ('2021-01-14', '2021-01-27'),
    '309e7c59a833d6b28c02769b7fb2758956f8170ce218d3fc34cd64709aaefb27':
        ('2021-01-28', '2021-02-10'),
    'e2d863435ec556e28fd763b2fe63e864bdec79c4610c99edd8865d59a1f394bd':
        ('2021-02-18', '2021-03-03'),
    'e4d98153166f1447a5fe63a023672b3f4caa0910c4e3278b3fe535b3a08f185c':
        ('2021-03-04', '2021-03-17'),
    '7da96c1c8c247748924968640087f4e2ffa009c11823851f637b9183731f0d62':
        ('2021-03-18', '2021-03-31'),
    '16987d5984a3c9cb1dfd95cedbb1d6391556f48abcefb15b15d9b5971c1071be':
        ('2021-04-01', '2021-04-15'),
    '96763d7ec669dce04fba22e42704f9922ff38dd3f76d08f86e613770ac0ce618':
        ('2021-04-16', '2021-04-29'),
    '5e666f29e79e579ecc0d9aed271757c6a7dedac8957aea7249ed4ce6f3a1ddd7':
        ('2021-04-30', '2021-05-18'),
    '2cb16c1e62265a3a27d1faa3bfb6c452250d04f3e3c244b7a3bd58b659bc761b':
        ('2021-05-19', '2021-06-01'),
    'd73a0925de8123993bf084579271d267a561508dab1be19c9946ec3bb4912076':
        ('2021-06-02', '2021-06-16'),
    'ff6577a09718112e9f3db250db2c77e894ca1adbdb7b8fb3cf6ef7e188ebaf1b':
        ('2021-06-17', '2021-06-30'),
    '9821d7e356a5cf1cd739d988079b29f4931e61fe94bee2e2b93c0a3a5aa03c1e':
        ('2021-07-01', '2021-07-14'),
    '39b50fcc2e9d4b570cdb49ba3a87f7bc522b4c343c424bb78b2a74ee6f41eb66':
        ('2021-07-15', '2021-07-28'),
    'c91ae7b9e92045751959d7a136a17e9a49e79486aa0c65fba5f2ea930cb6440e':
        ('2021-07-29', '2021-08-11'),
    '13133c993ea51c5fea988875e78f97f061cf724c33e50a67ac84a882c95302a7':
        ('2021-08-12', '2021-08-25'),
    'f40e62e40a8995193e2dbd90a00a71873f7bcac3c71116924a5daad9b526779e':
        ('2021-08-26', '2021-09-08'),
    'ce91051f37aed0f597e45b12c314b22f1c7a2f71b1f69e6f5e3a7d9b4c0e833f':
        ('2021-09-09', '2021-09-24'),
    'f983f29a177f17adc5a00cd8b253dfe9e49f423b4610bb5b3064b3ea8db3f737':
        ('2021-09-27', '2021-10-15'),
    '7cf8ff20abfb3ff388c1895ee3fa2f759318d25f57517b65dea2a140e613781d':
        ('2021-10-18', '2021-10-29'),
    'fea82e82944645d76127613063195fbff99bc6b1a3b795b845f76daf6c2f4a4a':
        ('2021-11-01', '2021-11-12'),
    '6ab0415408b779b13590779d642d55540896b645f5e58a686713f7229af7ec78':
        ('2021-11-15', '2021-11-26'),
    '74fa665e8d991d03259526953fe405ab6392f6ac05485b721e75bd99251a2afa':
        ('2021-11-29', '2021-12-10'),
    '14653821fc6e2ac68124adced95cb572d0e773b17e72deec38102dd17bb98ddd':
        ('2021-12-13', '2021-12-24'),
    '9890cbacd8f1443bf30d63ed15123d3aaca061d17f2821b8d498754d75df3f5c':
        ('2021-12-27', '2022-01-10'),
    'b8be8810cdd6ed409630d932fb9043e4bab738fed92f8bc5d46ea36d5c9c99bb':
        ('2022-01-11', '2022-01-24'),
    '0af81710f689dff9dd8d9e92d14d109bde74f75f901e3c53d0b16ee982f07fd2':
        ('2022-01-25', '2022-02-14'),
    '0322edd28f2b32c78dc0f468e325b8ce14f546b3ccc4a7c5242c6281b18cf0ff':
        ('2022-02-15', '2022-02-28'),
    '96331c83512dc39d53c2acde184b50d8f937aa8aa59d566b4fd42be239b2a4c4':
        ('2022-03-01', '2022-03-14'),
    'faa455836abb46dd475ff20d180b90ec18623c9ad8d90ada16618100fcdfeac1':
        ('2022-03-15', '2022-03-28'),
    'ea47ef13d8f04ebf26605493b4d061bece7d531b7346d9ee2ffa483b649a7d92':
        ('2022-03-29', '2022-04-13'),
    'a6ca6d1ed7e36124826369c38c91f23b9ef9f08fc666cc10fea22d66b8a2ab41':
        ('2022-04-14', '2022-04-27'),
    '6d67b38aebc75326f9fb6869c75e8ffd84c59152a6a29652f281508d90ce01a5':
        ('2022-04-28', '2022-05-16'),
    '96c4c881069dc36c79f557dcac62eb54010826631b4693ceec1b115834ec7a6d':
        ('2022-05-17', '2022-05-30'),
    '24f52ef27d27d628b6ce40e8aab8caa02e06ba9573d9ffc4aff5793523c9baa3':
        ('2022-05-31', '2022-06-14'),
    'd6298c525ee35dbe75a5032959f62d57122f5f1569df10be75fb42faadf84070':
        ('2022-06-15', '2022-06-28'),
    'da6b82929a27f3959e3f9cc632e0b8b4de0cc0adeb3738bb2836a4b759501a03':
        ('2022-06-29', '2022-07-12'),
    'd612a725e33e178c5d51bef1c396bdaba62f9c6561ec22efef586758d7d78a80':
        ('2022-07-13', '2022-07-26'),
    'ec7b51b6e6f07ac2fa88e8e1c103e7dd73c8ebd64b2041e032afef8ddbfe1a32':
        ('2022-07-27', '2022-08-09'),
    '92a89ef11ffcc06b492acc05cf31bbab1d8a40c6c9b12f49a22e31df6024cbeb':
        ('2022-08-10', '2022-08-23'),
    '4bb20873f0503ed9f9498ff7077d2d32485dad6b2c912c1a6e577074c1a086b8':
        ('2022-08-24', '2022-09-06'),
    '7b72577768883c6753f5fa309ab5ec8b592ff8efe9f0adc47fcadabd518e3dfb':
        ('2022-09-07', '2022-09-21'),
    '014cb6755135a30e7c179f67ef1fb7f5be3847a6ce77c3e3ca0f5bd6327d8c34':
        ('2022-09-22', '2022-10-12'),
    'c443aab49a4592cdb9afe4cd0f9d07c5d38c98a9e23af1fc6cf033c9b734dd2b':
        ('2022-10-13', '2022-10-26'),
    '1a0d9ab27d80ebe388dcd10dfeefac128a67bbc6362dd6ae3a4d600b1e26a9fb':
        ('2022-10-27', '2022-11-09'),
    '6b65ccdd0f8b86f2d17a33e14983a7e30403a2a27b6329a66d28b9cc2fd08916':
        ('2022-11-10', '2022-11-23'),
    '43ff02d6c69ae3d0f47b68893b219b462cfd72a57b6529516995ac1d455b140c':
        ('2022-11-24', '2022-12-07'),
    '38a2cfb9c5075aa7bc7a65b19d29404ab9bf12b7f410fa0e8add1d9eef0b7479':
        ('2022-12-08', '2022-12-21'),
    'd30b620ff80e6b5c5e111369eaae45624cd5caada60175eb2ad6cff3553d5560':
        ('2022-12-22', '2023-01-06'),
    '726b07808d4c0e1c21d70b64b36060e477645769412241d4009159c062c24273':
        ('2023-01-09', '2023-01-20'),
    '0892cb8f7d2ef047f898ba22e958072165b0ef10246b57ad72d9a8c4d92adda2':
        ('2023-01-30', '2023-02-10'),
    '7bd35541b96c16d8446a6ad46fe6fc6cd7e2c181a3462951902db2d7b1cead33':
        ('2023-02-13', '2023-02-24'),
    'b4fd5e96426ba2a4ae1743a4d88211b060d37c11262bf922aaf7cac5808d3339':
        ('2023-02-27', '2023-03-10'),
    '6edfd2e2eafe918bde20767df5d3ea151c6f2dc471ba2e01bfe7038ab79628ed':
        ('2023-03-13', '2023-03-24'),
    '2b41a66b29fce7ceeab3730decf078bcb309c68346422494e120452d65954aab':
        ('2023-03-27', '2023-04-10'),
    '02109d3ef17ef744f6213282cfff19aa2c3eafcd8eb26dc703104dd6a8ae57c8':
        ('2023-04-11', '2023-04-24'),
    '0de541cd8c2c5d614b939b75f3c4c74fc700a992afd80922cddb4db780cb4294':
        ('2023-04-25', '2023-05-11'),
    'bb64b06105d063015cb9f05c61cd339bca764fe8808ebca06e229f100b642a01':
        ('2023-05-12', '2023-05-25'),
    '53d286aacba859036165aca1524833a5f2e878c4ab0bb16992cb8efaceba2e55':
        ('2023-05-26', '2023-06-08'),
    'ad492653fa4016de389f0c656c0464db2b64daa7ad0f7c839431b9df9b42a77f':
        ('2023-06-09', '2023-06-26'),
    '9d4ba0427546afceb620359c0177fff5741930955c853199396b5f26f58b8273':
        ('2023-06-27', '2023-07-10'),
    'f431e1a2f05c14250739574c6fb7da121dd65176d49aeedf37a099bb6ee2139c':
        ('2023-07-11', '2023-07-24'),
    'ce522693cbdc644c2496f42aae00872bd5f0c1c928aee65dde524c5149c4fb2b':
        ('2023-07-25', '2023-08-07'),
    'e8fe33aaf0f6ef11779d11f82470ee2f8c8f3e76c289f50ae9c3f4b49cd75dad':
        ('2023-08-08', '2023-08-21'),
    '9fdea87815f42f880c65b75d3ea5e5e09eec14e8501c4b3d12d387683c49851c':
        ('2023-08-22', '2023-09-04'),
    '31de2c07d2f5dee661d82f9f838aac51a6b6c692bf5fd01543c9015d765f1740':
        ('2023-09-05', '2023-09-18'),
    '1f2cf28e2b6c4c720a6a50b7eeae385a974bc064dce1b51b3e1c972c668c05ce':
        ('2023-09-19', '2023-10-10'),
    '6cafdb8eb66302b3fcb079d154d029b827dbb384337311c0e644d8a0f6576d31':
        ('2023-10-11', '2023-10-24'),
    '014dee285bbc9379561dfccab981f4a08403b009863892637a329d9841d68d63':
        ('2023-10-25', '2023-11-07'),
    '06e2683b3b81c459a94fb0f93eb9cadfc50c9a4adc40615ea22cd8fefb9148ba':
        ('2023-11-08', '2023-11-21'),
    '6cbedf12ffa090317276491ee75eb5ca66b2fc5d1f3c6d32c5334de567f366ea':
        ('2023-11-22', '2023-12-05'),
    'ccddb86ede08ec56acd62c6156402433db8a5e68f50882055db5bc840ec1ef3d':
        ('2023-12-06', '2023-12-19'),
    'e41c1f3e7e0c7d7d609d5c71a0c54641cbd6b96e51e4df47a73ac78534ee22b8':
        ('2023-12-20', '2024-01-03'),
    'b458bbc87f664805ba233f526a8ebef57484caa0f815d51427f31e7dbf115f4c':
        ('2024-01-04', '2024-01-17'),
    '972b69c9f8c11498c0a9241a8e84fbf1a82c7fdc6a663f88d641a50b57056245':
        ('2024-01-18', '2024-01-31'),
    'dccf7617792291ca5a36185b7ee4fbec7d567b6c0ab347d08c063949cced49df':
        ('2024-02-01', '2024-02-22'),
    '7249e96181f2b9bb9b5075e5417f50fc6c498666c6048aee74ab91c220427009':
        ('2024-02-23', '2024-03-07'),
    'd254cbcf4bae9b850d2f2188413e76dfea7fb6d68d6ab4a8455a2b42d5adba1c':
        ('2024-03-08', '2024-03-21'),
    'dbc0f91d34a25f8ec2eee57b3260314790396f23747522672e4921c0c85457cd':
        ('2024-03-22', '2024-04-08'),
    '6781d0d629c6c9b8ac23927eda42af3be7dba42dd5ef6e96c7fbf537c9820c27':
        ('2024-04-09', '2024-04-22'),
    '1028bfe1fc7e95de10df69c89dae6d7f9979a1187a49ccd3eabcf838e74f1fcc':
        ('2024-04-23', '2024-05-09'),
    '17a11518fcab57725093ad9c538540085b96fd9aed66b0ef2c910943e067e174':
        ('2024-05-10', '2024-05-23'),
    '8ecad6a8360e9e9f8a29cb9711ea09743a06e461a6f0a2988dbda14d8f0f5d6b':
        ('2024-05-24', '2024-06-06'),
    '53d817d5664d424697e3e0bd98f3c2a1f4c6a54e7b99f27fac969b34f29b3ca9':
        ('2024-06-07', '2024-06-21'),
    '6c2c605a2148a9f9eded5ded3615bcf1159b6c14c44467048c0b95bcdbaf6453':
        ('2024-06-24', '2024-07-05'),
    'b5619adced660e89ee10d239d4a72845a75b4aed52b5df0b682447ae7870caaa':
        ('2024-07-08', '2024-07-19'),
    '1cffeb16c5b44d1e45d480e759ac49ab5c45fe999e344eaf04ce9ef57580cf51':
        ('2024-07-22', '2024-08-02'),
    '1f38120643511f9c7383753c2b135f8b88b8971b151b4a21b8ea4be53d776967':
        ('2024-08-05', '2024-08-16'),
    '8c33ba479f7b8a17f386bb36e5f7612795031d0798e8c5c177e95bfc57203a03':
        ('2024-08-19', '2024-08-30'),
    'a4d850033b906e598b96c9a9358d0d9d4f773b724bd82d8db02e1d998031294d':
        ('2024-09-02', '2024-09-13'),
    '82d389f0c501accf2fd550bc6844f3423aa76528be35e8cceb41c9a5f5bcd0f2':
        ('2024-09-18', '2024-10-08'),
    '0358ed12bc56dcc8dfd494484de9a84b0fd4c394f24d96f3b9af2ef6d65c315f':
        ('2024-10-09', '2024-10-22'),
    '1bf9087210522ad975fc33f493617ff21e352048b343a4bb47c794177637a653':
        ('2024-10-23', '2024-11-05'),
    '207a36fee74f6947478d58c56f30a289264e51a9ea46ce945409ae3f2185297c':
        ('2024-11-06', '2024-11-19'),
    'cfedbebf44031234e7afa2f2d5d7b687c2428eda3ddf193dfc9acd858c496745':
        ('2024-11-20', '2024-12-03'),
    'b00674fcad0207f1f803193dddcc9f2193121b2b1c409c73cb6a27590cdd3eee':
        ('2024-12-04', '2024-12-17'),
    'f14ea4378d82468fa7fc4218aaf0327612e227098ebd4bb390ccdfa1cbb2bce9':
        ('2024-12-18', '2024-12-31'),
    'c595678fd587506af5e5cfae0a43e5ad8950ab928e53a0bcfb435d782b477c34':
        ('2025-01-02', '2025-01-15'),
    '889536df7f626fd2104232189f8494536cc6f2e61819daffde3193e2e02832cd':
        ('2025-01-16', '2025-02-06'),
    '9725043820aaa0916cf21c5a22490deb9b5413e1ac70285da55c4dde14725750':
        ('2025-02-07', '2025-02-20'),
    '71241b89d9298736c502a546055b3c31bc2efe32b4f3dfc8b6e01f2275b78b7b':
        ('2025-02-21', '2025-03-06'),
    '8bc9e11be337a0f5f7f44171e7c83e2e30ff852a7ab60b2f57dcc08b4eb32d76':
        ('2025-03-07', '2025-03-20'),
    '8fa320eb4e69e860ac1f109e3be8de8e7b988bd9237451e5ca97e6c02ba58de8':
        ('2025-03-21', '2025-04-03'),
    '03f7a4e0cd8a26d19f10eda20d3b9ab89b98bdd021068f55498accf8eb0188be':
        ('2025-04-07', '2025-04-18'),
    '7ff68121d0f56b7cef6f78611c2196acdbaf4c1a1a59d9259c046a5a4ce1e43c':
        ('2025-04-21', '2025-05-07'),
    '7c5db78c40e26d66b13ac7ec065ef5ba85f94aa30a83ff1c4f32aaf977df1872':
        ('2025-05-08', '2025-05-21'),
    '92755b78267162802f1302daf089d73ee567d3c8f7b9469321cf84fa7fe1bb57':
        ('2025-05-22', '2025-06-05'),
    '5b69ed490cfa478308d0540d528beb7e4354ee0b73cce8933e2c9637b38cb046':
        ('2025-06-06', '2025-06-19'),
    'a436419c754fca04f6377227260ff87b011dd0e7bbc3fef8b62aa7705003c209':
        ('2025-06-20', '2025-07-03'),
    'f671222afe2167a84ff98e8b84369af22e82c6037558d9e2234d587b4b21f359':
        ('2025-07-04', '2025-07-17'),
    '292a1d439ff9d8eccbd204888e947f288bdaa1b6608d07297b7aa848f6a0d27a':
        ('2025-07-18', '2025-07-31'),
    '6c79e9d11221f407dbeb11e30b703fdd824e5d5d715ea21c371ba2c10a4c091a':
        ('2025-08-01', '2025-08-14'),
    '7c5a82f41723857758ff6b4edbb2599a0324319fbdc7e972b16ec58bc854679d':
        ('2025-08-15', '2025-08-28'),
    '7f0639a574c4167597dc77b06d6ac7de98b6a6ca5257b3b66db79131ea9a70e6':
        ('2025-08-29', '2025-09-11'),
    '2c8b84b75ef2be14a8b4fbc6d2f90207f2bcdf793fdfdc6d66c2c59899ced6bf':
        ('2025-09-12', '2025-09-25'),
    '5c9a3e8093911fde71cba7cd3e034e3ebcd5b306c8a1278019e864a10fbda31d':
        ('2025-09-26', '2025-10-17'),
    '167e0308630c9ff5b4b4cc3bce827d9d80d592ac77775363ef3ec66068c767a4':
        ('2025-10-20', '2025-10-31'),
    '0739f95bad2cb6a46826ed090249b5b6b53ab789287ad08afeb8d941a6857d69':
        ('2025-11-03', '2025-11-14'),
    'fce057637d7a75b84cb6d8ceb041ae5ac2ea478d70e1358b28d4c17c51103b67':
        ('2025-11-17', '2025-11-28'),
    '9ffe32a7599a0b1415219cfc12be62c5cebc81e934d4d7183270d368967de885':
        ('2025-12-01', '2025-12-12'),
    '5652cba29d40f0008a8d4631494f736fc94d8fe230a77d3c741e089f0c8713e2':
        ('2025-12-15', '2025-12-26'),
    'c9352e47099ece20a85bb6c00146e1ff15bffe7f180779ea2a72bf965070d865':
        ('2025-12-29', '2026-01-13'),
    '6613d10f8734752f1866156bbb04f00d760a65319a575691fb67921d53227388':
        ('2026-01-14', '2026-01-27'),
    'b89b6c27e5136fa5671ae4bc0d9a831385416370824b06d6548b32a69a8e5254':
        ('2026-01-28', '2026-02-10'),
    '29be6a2700dc77402103582a5c212d3dc0826d0decac5dcfb90e269cb559c51f':
        ('2026-02-11', '2026-03-04'),
    '2205bede9830e09db2b0a98091a52d27e8f69e17dd303da750c96da245bf7475':
        ('2026-03-05', '2026-03-19'),
    'cc03156ac50ec365129f8f69860fff88750ba5c7a6df0b8ecd604b62a223a395':
        ('2026-03-20', '2026-04-02'),
    'b595964438812c2c4282f9266b7cf233d214a43310d82997b4b55fe8074cbbc4':
        ('2026-04-03', '2026-04-17'),
    'bc0ac42eafff3e8675ec5788b577e101842955fb15e00af16db069cc458efc86':
        ('2026-04-20', '2026-05-06'),
    'e3da5764fd6da134fcbe1397b38a9d36793bd6dc04d82c6d93c778abb5da874e':
        ('2026-05-07', '2026-05-20'),
    'ea906f9deb3a5b9f10f988c0e56ea419644c551482e0b49a32c419156d73d0d4':
        ('2026-05-21', '2026-06-03'),
    '57da1a09eba033f41204dc88b3ab0cfffe6728b74212350a7071e7bd48fb24f1':
        ('2026-06-04', '2026-06-17'),
    'e9ea5e1d018a2da53e3126da701044e50ee883a65bfe2f7965d82d92952faea6':
        ('2026-06-18', '2026-07-02'),
    '6f2513ceb4549d59f955e7738efef6dc97bf464598677b77c6d12f2c0d14e8e2':
        ('2026-07-03', '2026-07-16'),
    'c4640308c44660eb51067accd4de60d0d4ce3b09df07d08b2fdeb6c61d3b1d21':
        ('2026-07-17', '2026-07-30'),
    'c5837af048ee66f8554e89d4e900fcb1f6b7174e64ec2299354b3813ab849e29':
        ('2026-07-31', '2026-08-13'),
    'e84fc8027e2ff51e5c9186d51527eafa6979cab3a2f5f7f68b54d86376ed580d':
        ('2026-08-14', '2026-08-27'),
    '9d52c6d88c76737b7dc318c46164fa97474819e0c6bb5728cfb751cd5873bf3a':
        ('2026-08-28', '2026-09-10'),
    '34739b020115822262a78c90c9eda71ad4ffc6c48160c7387b7ef40f91e8ba62':
        ('2026-09-11', '2026-09-18'),
    '048a2ced1a198e3b04bead63dfa380dcacbe2b26bc6f1b62cbc20621c1a3aee1': ('2021-11-29', '2022-01-10'),
    'd67dcdbb4b0b28dfe9184b7d3b8b052fa913b757766622b92af1fd8559213a9e': ('2022-01-11', '2022-02-28'),
    '818d05570cda794fb5f5cb6ddeea0cc71b335ebe1a52e13e5d5d1dc113ffa50b': ('2022-03-01', '2022-04-13'),
    'c57cc3ef839350830267139447af5d401635d5be3359458fddedca84c91f85c6': ('2022-04-14', '2022-05-30'),
    'c233a16b69260ce0e50c06182d925d70cb800d6f8985f21c1cb298a0abbbe7a0': ('2022-05-31', '2022-07-12'),
    'a4e6208abd9fbed2b00b528d8e5940ff3653bc6a0269bdcb884f457a51c5b240': ('2022-07-13', '2022-08-23'),
    '32734db87f149e3d786e77c06874e619e9b739dc953f09a8bfe2f4d40262b18a': ('2022-08-24', '2022-10-12'),
    '726cdd6dee65a9aa4ee546796eb913a6f57257157cf21c1cb1515ffe34e9aa8a': ('2022-10-13', '2022-11-23'),
    'a9819381a121577baf99a8eea20e4f5331fcc40b4aa08829ca078b6e5b5f80fd': ('2022-11-24', '2023-01-06'),
    '425f02b310af992b9cc4ce650237db79453550722c834fef9f0f3ddafd68641a': ('2023-01-09', '2023-02-24'),
    'd9bc20577bc4e3a5dff963ea3c4d3dd5f9552301ff0a52058ca92d40b722d616': ('2023-02-27', '2023-04-10'),
    'e3a61549d9dae230c110f5e256f82f04ff20b88a24124ccf390323cdd27eab0b': ('2023-04-11', '2023-05-25'),
    '72a6c66d24ad02ac47df055f41440da86ff01a78785d01272e5bfe269e0aa478': ('2023-05-26', '2023-07-10'),
    '868e05a2a385388e2381acebafccab257bc781323b3dc8a141de03fc3e5db4fe': ('2023-07-11', '2023-08-21'),
    '9914d19157f0f86a9b07932ae5ec40d0ce0e376ac698566d297156028911a39a': ('2023-08-22', '2023-10-10'),
    'e68202fd0ea3b640761d8e0b57e6bdc1e40b4aaa5bfe2a40770e7e98b5f9b321': ('2023-10-11', '2023-11-21'),
    '08f073000038a27309a8a5c7418d0b3e3d7224ab39c56e08cfe2c4f7ad44056d': ('2023-11-22', '2024-01-03'),
    '51515c23321c27556e0bcb3152815c7c77b9f8ee3cfac55b1ba6e7b89702d79a': ('2024-01-04', '2024-02-22'),
    '025f73cb57bb7b3b6ff591a1591dc5525781a644518b50b6b065f6c428a587c4': ('2024-02-23', '2024-04-08'),
    '2a986ee8f72a2664f4a1e564d170dfcfa4fd1c69de2ecd22c35d4ce3681d3243': ('2024-04-09', '2024-05-23'),
    '41595034644e876fe91ff311b0152428741b3a8d529d9ba1e905d48ad48d7669': ('2024-05-24', '2024-07-05'),
    'aa0bf73c8b478f3b901fc2a4eeec2268930213fb745199c95d2256386da706da': ('2024-07-08', '2024-08-16'),
    '9af5abd74b786189a093f7f6b775bb048b4c0b95d8ff56f9fcaf2015d8c16113': ('2024-08-19', '2024-10-08'),
    '10c39b96130d0017d64cffe0b26714a7cd9ee35cbc0f9b23edd5b54389cf43a3': ('2024-10-09', '2024-11-19'),
    '42592532cb3c27b4e8e404d929286d99971e71efd6e8915d6850d05a29a9539f': ('2024-11-20', '2024-12-31'),
    '3b964447809317358681f472766c5a07d6fed2417a27ed40a7d4ec0ab0262fa8': ('2025-01-02', '2025-02-20'),
    '76d63daf77de2cd51b2080d10d26aeb7b35c0c4ffac174883f668dd01c40c6c0': ('2025-02-21', '2025-04-03'),
    '06dbcc3270f1c1032e74f6860904260e5bd04a34092c41c7104b4e3ea6ae4309': ('2025-04-07', '2025-05-21'),
    '7b2b3ffacda3f9791f2e83cf33d9d3e6d45b1b31036352cd7f16662d7156de5e': ('2025-05-22', '2025-07-03'),
    '0a337d71ead9693ac5d239f059d1972ac6e037e0a3bc716ac8e1a0cae0b04881': ('2025-07-04', '2025-08-14'),
    'd09678a4a35fe212d05c71fe40975c269130c4a0788091ccef94a2046e61e447': ('2025-08-15', '2025-09-25'),
    '49c33c5fdfab77246da15e246c49d82a31ac671ec960b7286c02d5a8c9d9d762': ('2025-09-26', '2025-11-14'),
    '5a3d09de616c00a3754a42846854647608fd9ea409e3938fe79649dd9e395790': ('2025-11-17', '2025-12-26'),
    '4fdd9a8c06a2207c4bbb709ca58cefbaf791930d4a828b8ab722a99ed486cadd': ('2025-12-29', '2026-02-10'),
    '3d20399cdfc905e8d5d041cf86b9c85fe61470aa4305b0dddaf53710b3724e21': ('2026-02-11', '2026-04-02'),
    '2b36b323a8fcf8bb52b397028ef05fdc775c72ff0626f6d151077de1e8c376a9': ('2026-04-03', '2026-05-20'),
    '6b6795f951807a86f6188618b1b0d348edeb4bf155f8f05dfc7742be0b0837c6': ('2026-05-21', '2026-07-02'),
    '36e1176720c41510b190365ccc8e16bdc34900e4c3cf0bf51398cb6f6becbf13': ('2026-07-03', '2026-08-13'),
    '99751926abef71c30c50492b7c8f5aed7f0f026dfe9a83c5b2655558159d8fa6': ('2026-08-14', '2026-09-18'),
}
# User-authorized implementation parity only. Each source/window is reviewed
# separately; no generic minute strategy bypass or economic result adoption.
REVIEWED_MINUTE_PARITY = {
    # Frozen extended engineering cases: same N61 selection, explicit sizing/stop overrides.
    'eabd9c2c5a02409b027115273377b492dbb1d542b49b222bb5a88c17d140567a':
        ('2026-03-08', '2026-09-07'),
    '92cce3e31ef6f15253332702569606a356b47686b40d903ab5487717b0957961':
        ('2026-03-08', '2026-09-07'),
    '021015919e48c73d5d82da141b4dd033cf94dd854a8a7e6f1cb415b4823522af':
        ('2026-03-08', '2026-09-07'),
    'fa3571574cf9ce5823aebfd9bb6faab637be02145fbf7e52cacc5fe9ec2d0984':
        ('2026-03-08', '2026-09-07'),
    '22ebf3c92e5c9df26221cf3c577d4811cf04ab02b7021f835bff1ea259ede1b2':
        ('2026-03-08', '2026-03-13'),
    'f1022775016131db673650edbce928d8214a59aa36124c35ae59cb6e0e795dc7':
        ('2026-03-08', '2026-03-13'),
    '355e4223dba27054f3c47ef7a3f7a66d89c35dab25c0c53362986a849b9227fd':
        ('2026-03-08', '2026-03-20'),
    'b2f11c8af2ae7b46ffd7625b6fec9b39b79727ff1e7f664e08937d707026005c':
        ('2026-03-08', '2026-09-07'),
    '56924116c4cb126f494a5ece5352b0e2a5b6083684285af741a58787a0bf481c':
        ('2026-04-13', '2026-04-30'),
    '0f131f0f957133c96dc57269c70059b5e0e062c8ff017948fe15ced7f239b9a1':
        ('2026-06-03', '2026-06-05'),
}
_ACTIVE = None


class ControlError(ValueError):
    """A known refusal before a new research action."""


def require(condition, code):
    if not condition:
        raise ControlError(code)


def now():
    return datetime.now(timezone.utc).isoformat()


def inside(root, value):
    require(isinstance(value, str) and bool(value.strip()), 'missing_path')
    root = Path(root).resolve()
    path = (root / value).resolve()
    require(path != root and path.is_relative_to(root), 'path_outside_project')
    return path


def relative(root, path):
    return Path(path).resolve().relative_to(Path(root).resolve()).as_posix()


def sha(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, 'duplicate_json_key:' + key)
        result[key] = value
    return result


def read(path):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8-sig'),
                          object_pairs_hook=_pairs,
                          parse_constant=lambda value: (_ for _ in ()).throw(ControlError('nonfinite_json')))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ControlError('unreadable_json:' + Path(path).name) from exc


def bound(root, ref):
    require(isinstance(ref, dict), 'invalid_file_reference')
    path = inside(root, ref.get('path'))
    expected = ref.get('sha256')
    require(isinstance(expected, str) and re.fullmatch(r'[0-9a-f]{64}', expected) is not None,
            'invalid_sha256')
    try:
        require(sha(path) == expected, 'bound_file_changed:' + relative(root, path))
    except OSError as exc:
        raise ControlError('bound_file_missing:' + relative(root, path)) from exc
    return path


def load_state(root):
    data = read(inside(root, MASTER))
    require(isinstance(data, dict) and data.get('control_schema') in (SCHEMA, SCOPED_SCHEMA),
            'current_state_missing')
    require(type(data.get('control_revision')) is int and data['control_revision'] >= 1,
            'invalid_state_revision')
    current = data.get('current')
    require(isinstance(current, dict), 'invalid_current_state')
    if data['control_schema'] == SCOPED_SCHEMA:
        lines = current.get('execution_by_line')
        require(isinstance(lines, dict) and set(lines) == set(LINES), 'invalid_research_lines')
        resources = current.get('shared_resources', {})
        require(type(resources.get('max_inflight')) is int and resources['max_inflight'] > 0,
                'invalid_shared_concurrency_limit')
        require('execution' not in current, 'ambiguous_legacy_execution')
        for execution in lines.values():
            validate_execution(execution)
    else:
        require('execution_by_line' not in current, 'scoped_state_requires_v2')
        validate_execution(current.get('execution'))
    return data


def validate_execution(execution):
    require(isinstance(execution, dict), 'invalid_current_state')
    require(execution.get('status') in ('paused', 'active'), 'invalid_execution_status')
    require(isinstance(execution.get('attempts'), list), 'invalid_attempt_ledger')
    require(isinstance(execution.get('events'), list), 'invalid_control_events')
    for row in execution['attempts']:
        require(isinstance(row, dict) and all(isinstance(row.get(k), str) and row[k]
                for k in ('id', 'identity', 'plan_sha256', 'authorization_sha256', 'status')),
                'invalid_attempt_record')


def execution_for(current, line=None):
    """Select an owner explicitly; a legacy ledger has no inferred owner."""
    if 'execution_by_line' in current:
        require(line in LINES, 'research_line_required')
        return current['execution_by_line'][line]
    require(line is None, 'line_isolation_migration_required')
    return current['execution']


def all_attempts(current):
    if 'execution_by_line' in current:
        return [row for execution in current['execution_by_line'].values() for row in execution['attempts']]
    return current['execution']['attempts']


def attempt_owner(current, key, value):
    ledgers = current.get('execution_by_line', {None: current.get('execution')})
    matches = [(line, row) for line, execution in ledgers.items()
               for row in execution['attempts'] if row.get(key) == value]
    require(len(matches) == 1, 'missing_or_ambiguous_attempt_identity')
    return matches[0]


def bound_line(current, value, digest, binding_kind):
    require(isinstance(value, dict), 'invalid_line_bound_document')
    if 'execution_by_line' not in current:
        return None
    # Preserve frozen v1 files. Only exact hashes reviewed during migration get
    # a compatibility binding; all newly written plans/receipts must name a line.
    line = value.get('research_line')
    if line is None:
        line = current.get('legacy_line_bindings', {}).get(binding_kind, {}).get(digest)
    require(line in LINES, 'research_line_required')
    return line


@contextmanager
def _lock(root, wait_seconds=0):
    """Cooperative single writer; stale locks need inspection, never auto-expire."""
    path = inside(root, MASTER + '.control.lock')
    deadline = time.monotonic() + wait_seconds
    while True:
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            break
        except FileExistsError as exc:
            if time.monotonic() >= deadline:
                raise ControlError('state_busy_or_stale_lock_requires_inspection') from exc
            time.sleep(min(0.05, max(0, deadline - time.monotonic())))
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            handle.write(json.dumps({'pid': os.getpid(), 'created_at': now()}))
        yield
    finally:
        path.unlink()


def _save(root, data, expected_sha):
    path = inside(root, MASTER)
    require(sha(path) == expected_sha, 'state_changed_during_update')
    raw = json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + '\n'
    fd, name = tempfile.mkstemp(prefix='.research-state-', suffix='.tmp', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        require(sha(path) == expected_sha, 'state_changed_during_update')
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def update_state(root, expected_revision, change):
    with _lock(root):
        original_sha = sha(inside(root, MASTER))
        data = load_state(root)
        require(type(expected_revision) is int and data['control_revision'] == expected_revision,
                'state_revision_conflict')
        result = change(data['current'])
        data['control_revision'] += 1
        data['current']['updated_at'] = now()
        _save(root, data, original_sha)
        return result


def timestamp(value):
    try:
        parsed = datetime.fromisoformat(value)
        require(parsed.tzinfo is not None, 'timezone_required')
        return parsed
    except (TypeError, ValueError) as exc:
        raise ControlError('invalid_timestamp') from exc


def validate_cost_authorization(grant, line=None):
    """Existing points may fund one explicitly authorized main-line run only."""
    cost = grant.get('cost_class')
    require(cost in ('free', 'runtime_credits_authorized'), 'only_free_research_supported')
    if cost == 'free':
        return False
    require(line == 'main_strategy', 'runtime_credits_main_strategy_only')
    require(grant.get('allow_runtime_credit_confirm') is True
            and grant.get('credit_balance_precheck_required') is False
            and grant.get('purchase_or_recharge_authorized') is False
            and grant.get('paid_export_authorized') is False,
            'runtime_credit_scope_not_explicit')
    require(grant.get('max_submissions') == 1 and grant.get('max_inflight') == 1,
            'runtime_credit_single_run_required')
    require(re.fullmatch(r'[0-9a-f]{64}', str(grant.get('source_sha256', ''))) is not None,
            'runtime_credit_source_required')
    require(isinstance(grant.get('user_quote'), str) and bool(grant['user_quote'].strip()),
            'explicit_runtime_credit_instruction_required')
    return True


def validate_runtime_credit_scope(grant, grant_sha, info, line=None):
    enabled = validate_cost_authorization(grant, line)
    if not enabled:
        return False
    contract, run = info['contract'], info['run']
    reference = dict(path=info['plan']['authorization']['path'], sha256=grant_sha)
    require(info['plan']['authorization'] == reference
            and contract.get('execution_authorization') == reference,
            'runtime_credit_grant_not_bound')
    require(contract.get('source_sha256') == grant['source_sha256']
            and run.get('frequency') == 'tick' and run.get('capital') == 100000,
            'runtime_credit_source_or_account_mismatch')
    require(grant.get('window') == dict(start=run.get('start'), end=run.get('end')),
            'runtime_credit_window_not_authorized')
    return True


def safe_environment(info):
    result = dict(SAFE_ENV)
    if info.get('runtime_credit_authorized') is True:
        result['JQ_ALLOW_RUNTIME_CREDIT_CONFIRM'] = '1'
    return result


def authorization(root, current, line=None):
    execution = execution_for(current, line)
    pause_code = 'research_paused' if line is None else 'research_paused:' + line + ':new_submissions_only'
    require(execution['status'] == 'active', pause_code)
    ref = execution.get('authorization')
    require(ref is not None, 'line_authorization_not_registered')
    grant = read(bound(root, ref))
    require(bound_line(current, grant, ref['sha256'], 'authorizations') == line,
            'authorization_line_mismatch')
    require(isinstance(grant, dict) and grant.get('schema_version') == 'research_resume.v1'
            and grant.get('action') == 'resume_research', 'invalid_resume_receipt')
    require(isinstance(grant.get('user_quote'), str) and bool(grant['user_quote'].strip()),
            'explicit_user_resumption_required')
    validate_cost_authorization(grant, line)
    require(type(grant.get('max_submissions')) is int and grant['max_submissions'] > 0,
            'invalid_submission_budget')
    require(type(grant.get('max_inflight', 1)) is int and
            1 <= grant.get('max_inflight', 1) <= grant['max_submissions'], 'invalid_concurrency_budget')
    current_time = datetime.now(timezone.utc)
    require(timestamp(grant.get('issued_at')) <= current_time < timestamp(grant.get('expires_at')),
            'authorization_not_current')
    workspace = inside(root, grant.get('workspace'))
    require(workspace.is_relative_to((Path(root) / 'analysis').resolve()), 'research_workspace_required')
    return grant, ref['sha256'], workspace


def check_facts(root, refs):
    require(isinstance(refs, list), 'invalid_fact_references')
    for ref in refs:
        value = read(bound(root, ref))
        pointer = ref.get('pointer')
        require(isinstance(pointer, str) and pointer.startswith('/'), 'invalid_fact_pointer')
        try:
            for part in pointer[1:].split('/'):
                key = part.replace('~1', '/').replace('~0', '~')
                value = value[int(key)] if isinstance(value, list) else value[key]
        except (TypeError, ValueError, KeyError, IndexError) as exc:
            raise ControlError('fact_pointer_missing') from exc
        require('expected' in ref and type(value) is type(ref['expected']) and value == ref['expected'],
                'fact_claim_mismatch:' + pointer)


def validate_h781_admission(root, contract, policy):
    """Scope the new adapter without importing any exhausted H718 exception."""
    require({'path': contract.get('economic_source'),
             'sha256': contract.get('economic_source_sha256')} == H781_ECONOMIC_SOURCE
            and policy.get('economic_source') == H781_ECONOMIC_SOURCE,
            'h781_economic_source_changed')
    bound(root, H781_ECONOMIC_SOURCE)
    require(policy.get('effective_protocol') == contract.get('effective_protocol')
            and policy.get('execution_authorization') == contract.get('execution_authorization')
            and policy.get('inherited_exception_budget_transferred') is False
            and 'mechanical_repair_exception' not in policy,
            'h781_effective_binding_mismatch')
    protocol = read(bound(root, policy.get('effective_protocol')))
    require(protocol.get('first_preparation') == '2026-10-12T14:05:35.835673+00:00'
            and protocol.get('deadline') == '2026-10-12T14:35:35.835673+00:00'
            and protocol.get('deadline_is_checkpoint') is True
            and protocol.get('repairs_used') == 7 and protocol.get('remaining') is None
            and protocol.get('max_cumulative_mechanical_repairs') is None
            and protocol.get('prior_used_and_remaining') == {
                'H781': {'used': 2, 'remaining': 0}, 'H718': {'used': 5, 'remaining': 0}}
            and protocol.get('old_grants_refunded') is False
            and protocol.get('maximum_new_native_if_all_preconditions_pass') == 1
            and protocol.get('new_native') == protocol.get('new_accounts') == 0,
            'h781_clock_or_budget_changed')
    origin = read(bound(root, policy.get('instruction_origin')))
    require(origin.get('controller_message_is_not_authorization') is True
            and origin.get('old_H718_exceptions_not_reused') is True
            and any(row.get('quote') == '聚宽回测直接提，不看积分的'
                    for row in origin.get('human_messages', [])),
            'h781_human_instruction_origin_required')
    for key in ('compatibility', 'own_full_fixture', 'new_consumers'):
        require(read(bound(root, policy.get(key))).get('passed') is True,
                'h781_adapter_prerequisite_failed:' + key)
    extra = contract.get('additional_native_gates', {})
    require(extra.get('all_h781_source_score_morning_and_final_funding_consumers_required') is True
            and extra.get('unchanged_H667_activation_reported_separately') is True
            and extra.get('observations_not_complete_raw_tick_parity') is True,
            'h781_consumer_or_source_boundary_changed')


def validate_h781_observer_admission(root, contract, policy):
    """Bind the observer-only repair to the new human grant, without a refund."""
    validate_h781_admission(root, contract, policy)
    require(policy.get('original_native_policy') == H781_NATIVE_ADMISSION_POLICY
            and policy.get('old_native_remaining') == 0
            and policy.get('new_explicit_submission_budget') == 1
            and policy.get('old_grants_refunded') is False
            and policy.get('phase_cumulative_mechanical_repairs') == 8
            and policy.get('source_clock_and_old_budgets_preserved') is True,
            'h781_observer_old_budget_changed')
    grant = read(bound(root, policy.get('observer_resubmit_authorization')))
    quote = '授权这一次修补后的 H781 聚宽核验'
    require(policy.get('observer_resubmit_authorization') == contract.get('execution_authorization')
            and grant.get('user_quote') == quote
            and grant.get('source_sha256') == H781_OBSERVER_ADMISSION_SOURCE
            and grant.get('max_submissions') == grant.get('max_inflight') == 1
            and grant.get('previous_used') == 1 and grant.get('previous_remaining') == 0
            and grant.get('old_grants_refunded') is False
            and grant.get('first_clock_reset') is False
            and grant.get('window') == {'start': '2020-01-01', 'end': '2025-12-31'},
            'h781_observer_exact_grant_required')
    origin = read(bound(root, policy.get('human_resubmit_origin')))
    require(grant.get('instruction_origin') == policy.get('human_resubmit_origin')
            and origin.get('direct_human_quote') == quote
            and origin.get('human_reply_visible_in_current_task') is True
            and origin.get('controller_message_is_not_authorization') is True
            and origin.get('old_grants_refunded') is False
            and origin.get('one_additional_native_only') is True
            and origin.get('previously_presented_payload') == {
                'source': {'path': contract.get('source'), 'sha256': H781_OBSERVER_ADMISSION_SOURCE},
                'start': '2020-01-01', 'end': '2025-12-31', 'capital': 100000,
                'frequency': 'tick', 'original_costs': True},
            'h781_observer_direct_human_origin_required')
    proof = read(bound(root, policy.get('observer_status_compatibility')))
    reuse = read(bound(root, policy.get('observer_source_reuse')))
    require(proof.get('passed') is True
            and proof.get('source_sha256') == H781_OBSERVER_ADMISSION_SOURCE
            and proof.get('same_string_payload_days') == proof.get('native_status_injected_days') == 1455
            and all(proof.get(key) is True for key in (
                'source_only_observer_AST_changed', 'state_and_nonstatus_fields_unchanged',
                'morning_callback_exactly_once', 'unrelated_unsupported_objects_still_rejected'))
            and proof.get('full_native_runtime_verified') is False
            and reuse.get('passed') is True
            and reuse.get('source') == {'path': contract.get('source'), 'sha256': H781_OBSERVER_ADMISSION_SOURCE}
            and reuse.get('changed_functions') == ['H781_observe_morning']
            and reuse.get('unchanged_function_AST') == 169,
            'h781_observer_repair_scope_changed')
    protocol = read(bound(root, policy.get('observer_status_protocol')))
    failure = read(bound(root, policy.get('previous_native_failure')))
    require(protocol.get('phase_cumulative_mechanical_repairs') == 8
            and protocol.get('first_clock_reset') is False
            and protocol.get('native_submissions_used') == 1 and protocol.get('native_remaining') == 0
            and protocol.get('new_native') == protocol.get('new_local_accounts') == 0
            and failure.get('terminal_failed') is True
            and failure.get('source_exact') is True
            and failure.get('submitted_native_tasks') == 1
            and set(failure.get('source_hashes', {}).values()) == {H781_NATIVE_ADMISSION_SOURCE},
            'h781_observer_original_failure_or_clock_changed')


def validate_external_read_only_inputs(root, contract):
    """Only this pinned H781 input set may resolve outside the workspace."""
    approved = next(((source, binding) for source, policy, binding in (
        (H781_NATIVE_ADMISSION_SOURCE, H781_NATIVE_ADMISSION_POLICY, H781_EXTERNAL_READ_ONLY_BINDING),
        (H781_OBSERVER_ADMISSION_SOURCE, H781_OBSERVER_ADMISSION_POLICY, H781_OBSERVER_READ_ONLY_BINDING))
        if contract.get('source_sha256') == source
        and contract.get('native_admission_policy') == policy
        and contract.get('external_read_only_binding') == binding), None)
    require(approved is not None, 'external_read_only_inputs_not_reviewed')
    source, binding = approved
    manifest = read(bound(root, binding))
    require(manifest.get('source_sha256') == source
            and manifest.get('read_only') is True and manifest.get('new_acquisition') is False
            and manifest.get('no_restoration_or_copy') is True
            and contract.get('external_read_only_inputs') == manifest.get('external_read_only_inputs'),
            'external_read_only_input_scope_changed')
    for value, expected in manifest['external_read_only_inputs'].items():
        path = Path(value)
        require(path.is_absolute() and sha(path) == expected, 'external_read_only_input_changed')
    for logical, ref in manifest['archived_logical_inputs'].items():
        path = (Path(root) / logical).resolve()
        require(str(path) == ref['path'] and sha(path) == ref['sha256'],
                'archived_input_relocation_changed')


def compact_prefix_gates(root, contract, expected_ref):
    """Only the hash-approved 1455-day prefix of the original comparator."""
    scope_ref = {'path': COMPACT_PREFIX_SCOPE, 'sha256': COMPACT_PREFIX_SCOPE_SHA256}
    require(contract.get('baseline_scope') == scope_ref, 'fixed_compact_prefix_required')
    scope = read(bound(root, scope_ref))
    require(scope.get('schema_version') == 'fixed_compact_native_prefix_scope.v1'
            and scope.get('baseline_lock') == expected_ref
            and scope.get('independent_prefix_verified') is True
            and scope.get('independent_validation') is False, 'compact_prefix_proof_required')
    for path, digest in scope['locked_inputs'].items():
        bound(root, {'path': path, 'sha256': digest})
    bound(root, scope['original_source'])
    run = contract.get('run', {})
    require({key: run.get(key) for key in ('start', 'end')} == scope['platform_window']
            and run.get('days') == scope['days'] == 1455
            and run.get('capital') == scope['capital'] == 100000,
            'compact_goal_window_mismatch')
    require(contract.get('comparison_costs') == scope['costs'], 'compact_goal_costs_mismatch')
    extra = contract.get('additional_native_gates', {})
    require(extra.get('original_relative_annual_minimum') == scope['relative_annual_minimum']
            and extra.get('original_relative_drawdown_reduction_minimum') == scope['relative_drawdown_reduction_minimum'],
            'compact_prefix_native_gate_changed')
    if 'native_admission_policy' in contract:
        approved = next(((reference, source) for reference, source in (
            (H718_NATIVE_ADMISSION_POLICY, H718_NATIVE_ADMISSION_SOURCE),
            (H718_DATE_COMPAT_ADMISSION_POLICY, H718_DATE_COMPAT_ADMISSION_SOURCE),
            (H718_NAMESPACE_COMPAT_ADMISSION_POLICY, H718_NAMESPACE_COMPAT_ADMISSION_SOURCE),
            (H781_NATIVE_ADMISSION_POLICY, H781_NATIVE_ADMISSION_SOURCE),
            (H781_OBSERVER_ADMISSION_POLICY, H781_OBSERVER_ADMISSION_SOURCE))
            if contract['native_admission_policy'] == reference), None)
        require(approved is not None, 'unapproved_native_admission_policy')
        policy_ref, approved_source = approved
        policy = read(bound(root, policy_ref))
        require(contract.get('source_sha256') == approved_source == policy.get('source_sha256')
                and policy.get('research_line') == 'main_strategy'
                and policy.get('gate_profile') == 'fixed_original_relative_10_10_ms028'
                and policy.get('result_known_when_prepared') is False
                and policy.get('no_prior_verdict_rejudged') is True,
                'native_admission_policy_source_or_role_mismatch')
        if policy_ref == H718_DATE_COMPAT_ADMISSION_POLICY:
            exception = read(bound(root, policy.get('mechanical_repair_exception')))
            require(exception.get('user_quote') == '允许这1次兼容修复及1次聚宽回测'
                    and exception.get('research_line') == 'main_strategy'
                    and exception.get('action') == 'one_extra_date_compatibility_repair_and_one_native_task'
                    and exception.get('scope') == 'two_date_calls_only'
                    and exception.get('max_extra_repairs') == exception.get('max_native_tasks') == 1
                    and exception.get('old_repairs_used') == 2 and exception.get('old_remaining') == 0
                    and exception.get('max_cumulative_repairs') == 3
                    and exception.get('source_sha256') == approved_source
                    and exception.get('original_source_sha256') == H718_NATIVE_ADMISSION_SOURCE
                    and exception.get('old_grants_refunded') is False,
                    'date_compatibility_exception_scope_mismatch')
            failed = read(bound(root, exception.get('original_failed_task')))
            require(failed.get('schema_version') == 'research_reconciliation.v1'
                    and failed.get('outcome') == 'failed'
                    and failed.get('source_sha256') == H718_NATIVE_ADMISSION_SOURCE
                    and exception.get('window') == scope['platform_window']
                    and exception.get('capital') == scope['capital'],
                    'date_compatibility_original_failure_or_account_mismatch')
        elif policy_ref == H718_NAMESPACE_COMPAT_ADMISSION_POLICY:
            exception = read(bound(root, policy.get('mechanical_repair_exception')))
            require(exception.get('user_quote') == '允许这1次兼容修复及1次聚宽回测'
                    and exception.get('research_line') == 'main_strategy'
                    and exception.get('action') == 'one_extra_namespace_compatibility_repair_and_one_native_task'
                    and exception.get('scope') == 'two_all_calls_only'
                    and exception.get('max_extra_repairs') == exception.get('max_native_tasks') == 1
                    and exception.get('old_repairs_used') == 3 and exception.get('old_remaining') == 0
                    and exception.get('max_cumulative_repairs') == 4
                    and exception.get('source_sha256') == approved_source
                    and exception.get('original_source_sha256') == H718_DATE_COMPAT_ADMISSION_SOURCE
                    and exception.get('old_grants_refunded') is False,
                    'namespace_compatibility_exception_scope_mismatch')
            failed = read(bound(root, exception.get('original_failed_task')))
            require(failed.get('schema_version') == 'research_reconciliation.v1'
                    and failed.get('outcome') == 'failed'
                    and failed.get('source_sha256') == H718_DATE_COMPAT_ADMISSION_SOURCE
                    and exception.get('window') == scope['platform_window']
                    and exception.get('capital') == scope['capital'],
                    'namespace_compatibility_original_failure_or_account_mismatch')
        if policy_ref == H781_NATIVE_ADMISSION_POLICY:
            validate_h781_admission(root, contract, policy)
        elif policy_ref == H781_OBSERVER_ADMISSION_POLICY:
            validate_h781_observer_admission(root, contract, policy)
        require(policy.get('window') == scope['platform_window']
                and policy.get('days') == scope['days'] and policy.get('capital') == scope['capital']
                and policy.get('original_relative_annual_minimum') == scope['relative_annual_minimum']
                and policy.get('original_relative_drawdown_reduction_minimum') == scope['relative_drawdown_reduction_minimum']
                and policy.get('raw_drawdown_max') == '0.20'
                and extra.get('ms028_raw_drawdown_max') == '.20'
                and extra.get('h244_comparison_role') == 'reported_reference'
                and 'annual_strictly_above_h244_native' not in extra
                and 'raw_drawdown_strictly_below_h244_native' not in extra,
                'native_admission_policy_gate_mismatch')
        require(bound(root, policy.get('current_target')) == inside(root, 'docs/当前研究目标.md')
                and bound(root, policy.get('acceptance')) == inside(root, ACCEPTANCE),
                'native_admission_policy_authority_mismatch')
    else:
        require(extra.get('annual_strictly_above_h244_native') is True
                and extra.get('raw_drawdown_strictly_below_h244_native') is True,
                'compact_prefix_native_gate_changed')
    metrics = scope['original_metrics']
    return {
        'full_net_annualized_return_strictly_above': metrics['net_annualized_return_250'],
        'full_raw_daily_drawdown_strictly_below': metrics['raw_daily_max_drawdown'],
    }


def validate_economic_gates(root, plan, contract):
    """Keep legacy contracts intact; new main-line goals bind the fixed comparator."""
    version = contract.get('economic_gate_version', 'legacy_challenge.v1')
    if version == 'legacy_challenge.v1':
        expected_gates = GATES
    else:
        require(version in (COMPACT_GATE_VERSION, COMPACT_FULL_GATE_VERSION),
                'unknown_economic_gate_version')
        require(plan.get('research_line') == 'main_strategy', 'compact_gates_main_strategy_only')
        expected_ref = {'path': COMPACT_BASELINE, 'sha256': COMPACT_BASELINE_SHA256}
        require(contract.get('baseline_lock') == expected_ref, 'fixed_compact_baseline_required')
        baseline_path = bound(root, expected_ref)
        if 'baseline_scope' in contract:
            require(version == COMPACT_FULL_GATE_VERSION, 'compact_prefix_full_gate_only')
            expected_gates = compact_prefix_gates(root, contract, expected_ref)
        else:
            baseline = read(baseline_path)
            expected_gates = {}
            gate_windows = ('full', 'recent') if version == COMPACT_GATE_VERSION else ('full',)
            for window in gate_windows:
                metrics = baseline['accounts']['compact'][window]['metrics']
                expected_gates[window + '_net_annualized_return_strictly_above'] = metrics['net_annualized_return_250']
                expected_gates[window + '_raw_daily_drawdown_strictly_below'] = metrics['raw_daily_max_drawdown']
            run = contract.get('run', {})
            windows = [baseline['accounts']['compact'][window]['window'] for window in gate_windows]
            require(any(run.get('start') == window['start'] and run.get('end') == window['end']
                        for window in windows), 'compact_goal_window_mismatch')
            require(contract.get('comparison_costs') == baseline['costs'], 'compact_goal_costs_mismatch')
    gates = contract.get('economic_gates')
    require(isinstance(gates, dict), 'economic_gates_required')
    if version in (COMPACT_GATE_VERSION, COMPACT_FULL_GATE_VERSION):
        require(set(gates) == set(expected_gates), 'compact_gate_keys_changed')
    for key, expected in expected_gates.items():
        try:
            value = Decimal(str(gates.get(key)))
            require(value.is_finite() and value == Decimal(expected), 'economic_gate_changed:' + key)
        except InvalidOperation as exc:
            raise ControlError('invalid_economic_gate:' + key) from exc


def inspect_plan(root, plan_path):
    """Read-only binding check. Does not approve profit or independent validation."""
    plan_path = inside(root, str(plan_path))
    plan = read(plan_path)
    require(isinstance(plan, dict) and plan.get('schema_version') == 'research_launch.v1', 'invalid_launch_plan')
    for field in ('experiment_id', 'question', 'on_pass', 'on_fail'):
        require(isinstance(plan.get(field), str) and bool(plan[field].strip()), 'missing_' + field)
    require(re.fullmatch(r'[A-Za-z0-9_-]{1,100}', plan['experiment_id']) is not None, 'invalid_experiment_id')
    require(plan.get('data_role') == 'used_development' and plan.get('independent_validation') is False,
            'v1_supports_development_only_not_promotion')
    require(bound(root, plan.get('acceptance')) == inside(root, ACCEPTANCE), 'wrong_acceptance_document')
    check_facts(root, plan.get('facts', []))
    contract_path = bound(root, plan.get('contract'))
    contract = read(contract_path)
    require(isinstance(contract, dict), 'invalid_contract')
    source = bound(root, {'path': contract.get('source'), 'sha256': contract.get('source_sha256')})
    require(source.suffix == '.py', 'python_source_required')
    if 'economic_source' in contract:
        bound(root, {'path': contract['economic_source'], 'sha256': contract.get('economic_source_sha256')})
    locked = contract.get('locked_inputs')
    require(isinstance(locked, dict) and bool(locked), 'locked_inputs_required')
    for path, digest in locked.items():
        bound(root, {'path': path, 'sha256': digest})
    if 'external_read_only_inputs' in contract:
        validate_external_read_only_inputs(root, contract)
    require(contract.get('local_check') == 'pass', 'local_check_failed')
    receipts = contract.get('prerequisite_receipts')
    require(isinstance(receipts, list) and bool(receipts), 'prerequisite_receipts_required')
    for name in receipts:
        require(isinstance(name, str) and name in locked, 'receipt_not_hash_bound')
        receipt = read(bound(root, {'path': name, 'sha256': locked[name]}))
        require(isinstance(receipt, dict) and receipt.get('decision') == 'pass'
                and receipt.get('source_sha256') == contract['source_sha256'], 'prerequisite_failed_or_wrong_source')
    validate_economic_gates(root, plan, contract)
    run = contract.get('run')
    require(isinstance(run, dict), 'single_run_contract_required')
    try:
        start, end = date.fromisoformat(run['start']), date.fromisoformat(run['end'])
        require(start <= end and start.isoformat() == run['start'] and end.isoformat() == run['end'],
                'invalid_window')
    except (KeyError, TypeError, ValueError) as exc:
        raise ControlError('invalid_window') from exc
    require(type(run.get('capital')) is int and run['capital'] == 100000,
            'capital_or_frequency_mismatch')
    kind = plan.get('execution_kind', 'strategy_backtest')
    require(kind in ('strategy_backtest', 'zero_order_minute_collection', 'minute_parity_baseline'), 'invalid_execution_kind')
    if kind == 'zero_order_minute_collection':
        require(run.get('frequency') == 'minute' and
                REVIEWED_MINUTE_COLLECTIONS.get(contract['source_sha256']) == (run['start'], run['end']),
                'minute_collection_requires_reviewed_source_and_window')
    elif kind == 'minute_parity_baseline':
        require(run.get('frequency') == 'minute' and
                REVIEWED_MINUTE_PARITY.get(contract['source_sha256']) == (run['start'], run['end']),
                'minute_parity_requires_reviewed_source_and_window')
    else:
        require(run.get('frequency') == 'tick', 'capital_or_frequency_mismatch')
    require(isinstance(run.get('slot'), str) and re.fullmatch(r'[A-Za-z0-9_-]{1,120}', run['slot']) is not None,
            'invalid_slot')
    output = inside(root, run.get('output'))
    identity_fields = [contract['source_sha256'], run['start'], run['end'], '100000', run['frequency']]
    identity = hashlib.sha256(json.dumps(identity_fields).encode()).hexdigest()
    return {'plan': plan, 'plan_path': plan_path, 'plan_sha256': sha(plan_path), 'contract': contract,
            'source': source, 'output': output, 'identity': identity, 'run': run}


def preflight(root, plan_path):
    data = load_state(root)
    current = data['current']
    path = inside(root, str(plan_path))
    line = bound_line(current, read(path), sha(path), 'plans')
    grant, grant_sha, workspace = authorization(root, current, line)
    info = inspect_plan(root, plan_path)
    credit = validate_runtime_credit_scope(grant, grant_sha, info, line)
    if (line == 'main_strategy' and
            current['research'].get('objective', {}).get('id') == 'structure_research_20260920'):
        expected_version = current['research']['objective'].get('economic_gate_version', COMPACT_GATE_VERSION)
        require(info['contract'].get('economic_gate_version') == expected_version,
                'current_main_strategy_requires_compact_goal')
    require(bound_line(current, info['plan'], info['plan_sha256'], 'plans') == line,
            'plan_line_changed_during_preflight')
    require(info['source'].is_relative_to(workspace) and info['output'].is_relative_to(workspace)
            and info['plan_path'].is_relative_to(workspace), 'outside_authorized_workspace')
    require(not info['output'].exists(), 'existing_output_reconcile_do_not_resubmit')
    attempts = all_attempts(current)
    require(not any(a['identity'] == info['identity'] and a['status'] != 'confirmed_not_submitted'
                    for a in attempts), 'existing_attempt_reconcile_do_not_resubmit')
    require(not any(a.get('experiment_id') == info['plan']['experiment_id'] for a in attempts),
            'experiment_id_already_used')
    require(not any(a.get('slot') == info['run']['slot'] and a['status'] not in TERMINAL
                    for a in attempts), 'slot_in_use_requires_reconciliation')
    own_attempts = execution_for(current, line)['attempts']
    require(sum(a['status'] not in TERMINAL for a in own_attempts) < grant.get('max_inflight', 1),
            'inflight_limit_or_unknown_attempt_requires_reconciliation')
    if line is not None:
        require(sum(a['status'] not in TERMINAL for a in attempts) < current['shared_resources']['max_inflight'],
                'shared_inflight_limit_requires_reconciliation')
    used = sum(a['authorization_sha256'] == grant_sha for a in own_attempts)
    require(used < grant['max_submissions'], 'submission_budget_exhausted')
    info.update(revision=data['control_revision'], authorization_sha256=grant_sha,
                research_line=line, runtime_credit_authorized=credit)
    return info


def parse_launch(argv):
    values, flags, positional = {}, set(), []
    index = 0
    while index < len(argv):
        item = argv[index]
        if item in VALUE_FLAGS:
            require(item not in values and index + 1 < len(argv) and not argv[index + 1].startswith('--'),
                    'missing_or_duplicate_flag:' + item)
            values[item] = argv[index + 1]
            index += 2
        elif item in BOOLEAN_FLAGS:
            require(item not in flags, 'duplicate_flag:' + item)
            flags.add(item)
            index += 1
        else:
            require(not item.startswith('-'), 'unsupported_launch_flag:' + item)
            positional.append(item)
            index += 1
    require(len(positional) == 3, 'explicit_source_and_window_required')
    require(REQUIRED_FLAGS.issubset(flags), 'safe_tick_flags_required')
    require(len(flags & {'--tick', '--minute'}) == 1, 'explicit_single_frequency_required')
    require('--research-plan' in values, 'research_plan_required')
    return positional, values


def verify_invocation(root, info, argv, environ):
    positional, values = parse_launch(argv)
    frequency = 'tick' if '--tick' in argv else 'minute'
    require(frequency == info['run']['frequency'], 'launch_frequency_mismatch')
    require(inside(root, positional[0]) == info['source'] and positional[1:] == [info['run']['start'], info['run']['end']],
            'launch_source_or_window_mismatch')
    require(values.get('--slot') == info['run']['slot'] and
            inside(root, values.get('--output-dir')) == info['output'], 'launch_slot_or_output_mismatch')
    require(inside(root, values['--research-plan']) == info['plan_path'], 'launch_plan_mismatch')
    for key, value in safe_environment(info).items():
        require(environ.get(key) == value, 'unsafe_environment:' + key)


def reserve(root, info):
    ticket = {'id': uuid.uuid4().hex, 'identity': info['identity'],
              'experiment_id': info['plan']['experiment_id'], 'plan_path': relative(root, info['plan_path']),
              'plan_sha256': info['plan_sha256'], 'authorization_sha256': info['authorization_sha256'],
              'output': relative(root, info['output']), 'slot': info['run']['slot'],
              'status': 'reserved', 'created_at': now()}
    if info['research_line'] is not None:
        ticket['research_line'] = info['research_line']
    def change(current):
        # Recheck all bindings while holding the state lock, before reserving budget.
        fresh = preflight(root, info['plan_path'])
        require(fresh['plan_sha256'] == info['plan_sha256'], 'plan_changed_before_reservation')
        require(fresh['authorization_sha256'] == info['authorization_sha256'], 'authorization_changed_before_reservation')
        execution_for(current, info['research_line'])['attempts'].append(ticket)
    update_state(root, info['revision'], change)
    return ticket


def record_exit(root, ticket, status):
    # Exit is local evidence only; even zero does not prove platform completion.
    for _ in range(3):
        data = load_state(root)
        def change(current):
            row = next((a for a in execution_for(current, ticket.get('research_line'))['attempts']
                        if a['id'] == ticket['id']), None)
            require(row is not None and row['status'] == 'reserved', 'attempt_changed_requires_reconciliation')
            row.update(status=status, local_exit_at=now())
        try:
            update_state(root, data['control_revision'], change)
            return
        except ControlError as exc:
            if str(exc) != 'state_revision_conflict':
                raise
    raise ControlError('exit_record_conflict_requires_reconciliation')


def require_snapshot(source_path, source_bytes):
    require(_ACTIVE is not None, 'missing_active_launch_ticket')
    root, info, _ticket = _ACTIVE
    require(inside(root, str(source_path)) == info['source'] and
            hashlib.sha256(source_bytes).hexdigest() == info['contract']['source_sha256'],
            'source_changed_after_preflight')


def before_browser():
    require(_ACTIVE is not None, 'missing_active_launch_ticket')
    root, info, ticket = _ACTIVE
    data = load_state(root)
    line = ticket.get('research_line')
    _grant, grant_sha, _workspace = authorization(root, data['current'], line)
    require(grant_sha == ticket['authorization_sha256'], 'authorization_changed_before_browser')
    require(any(a['id'] == ticket['id'] and a['status'] == 'reserved'
                for a in execution_for(data['current'], line)['attempts']), 'attempt_no_longer_reserved')
    fresh = inspect_plan(root, info['plan_path'])
    require(fresh['plan_sha256'] == ticket['plan_sha256'], 'plan_changed_before_browser')
    validate_runtime_credit_scope(_grant, grant_sha, fresh, line)


def guarded_main(root):
    def decorate(function):
        @functools.wraps(function)
        def guarded(*args, **kwargs):
            global _ACTIVE
            try:
                current = load_state(root)['current']
                if 'execution_by_line' not in current:
                    authorization(root, current)
                _positional, values = parse_launch(sys.argv[1:])
                info = preflight(root, values['--research-plan'])
                verify_invocation(root, info, sys.argv[1:], os.environ)
                require(_ACTIVE is None, 'nested_launch_forbidden')
                ticket = reserve(root, info)
            except ControlError as exc:
                print(json.dumps({'submitted': False, 'control_pass': False, 'reason': str(exc)}, ensure_ascii=False))
                raise SystemExit(2) from exc
            _ACTIVE = (root, info, ticket)
            status = 'runner_returned_requires_reconciliation'
            try:
                return function(*args, **kwargs)
            except BaseException:
                status = 'runner_error_requires_reconciliation'
                raise
            finally:
                _ACTIVE = None
                try:
                    record_exit(root, ticket, status)
                except (ControlError, OSError) as exc:
                    print(json.dumps({'attempt': ticket['id'], 'exit_recorded': False,
                                      'reason': str(exc), 'resubmit_allowed': False}, ensure_ascii=False))
        return guarded
    return decorate


@contextmanager
def submit_guard():
    """Pause and the sole click are serialized. A completed click is not cancellable.

    Commit before invoking the browser; exceptions or process death retain the
    attempt. Holding this short-lived lock means a concurrent pause either takes
    effect before this click, or reports busy and must be retried after it.
    """
    require(_ACTIVE is not None, 'missing_active_launch_ticket')
    root, _info, ticket = _ACTIVE
    with _lock(root, wait_seconds=6):
        original_sha = sha(inside(root, MASTER))
        before_browser()
        data = load_state(root)
        row = next(a for a in execution_for(data['current'], ticket.get('research_line'))['attempts']
                   if a['id'] == ticket['id'])
        require('submission_invoked_at' not in row, 'submission_already_invoked_no_retry')
        row['submission_invoked_at'] = now()
        data['control_revision'] += 1
        data['current']['updated_at'] = now()
        _save(root, data, original_sha)
        yield


def classify_result(checks):
    """Classify supplied evidence, never infer correctness from a good profit."""
    names = ('correctness', 'development_improvement', 'total_goal_gates', 'independent_validation')
    require(isinstance(checks, dict) and set(checks) == set(names), 'result_checks_required')
    require(all(value is None or type(value) is bool for value in checks.values()), 'invalid_result_check')
    correct = checks['correctness'] is True
    return {
        'correctness': 'passed' if correct else ('failed' if checks['correctness'] is False else 'unknown'),
        'development': 'improved_not_adopted' if correct and checks['development_improvement'] is True
                       else 'no_verified_improvement',
        'total_goal': 'passed' if correct and checks['total_goal_gates'] is True else 'not_passed',
        'adoption': 'eligible_for_review_not_auto_deployed' if correct and
                    checks['total_goal_gates'] is True and checks['independent_validation'] is True
                    else 'not_eligible',
    }


def record_result(root, expected_revision, receipt_path):
    """Update the current result pointer only. Never resume or reset reflection."""
    path = inside(root, str(receipt_path))
    ref = {'path': relative(root, path), 'sha256': sha(path)}
    receipt = read(bound(root, ref))
    require(isinstance(receipt, dict) and receipt.get('schema_version') == 'research_result.v1',
            'invalid_result_receipt')
    classification = classify_result(receipt.get('checks'))
    require(isinstance(receipt.get('experiment_id'), str) and bool(receipt['experiment_id']), 'experiment_id_required')
    require(isinstance(receipt.get('question'), str) and bool(receipt['question'].strip()), 'result_question_required')
    require(isinstance(receipt.get('facts'), list) and bool(receipt['facts']), 'result_facts_required')
    check_facts(root, receipt['facts'])
    def change(current):
        line, row = attempt_owner(current, 'experiment_id', receipt['experiment_id'])
        require(line in (None, 'main_strategy'), 'engineering_result_not_main_strategy_result')
        require(row['status'] in ('completed', 'failed'), 'result_requires_reconciled_attempt')
        require(row['plan_sha256'] == receipt.get('plan_sha256'), 'wrong_result_identity')
        # v1 launch plans are explicitly development-only. A self-described
        # independent-validation pass cannot promote this experiment.
        require(receipt['checks']['independent_validation'] is not True,
                'development_attempt_cannot_be_independent_validation')
        current['research']['latest_result'] = dict(ref, experiment_id=receipt['experiment_id'],
                                                   classification=classification)
        execution_for(current, line)['events'].append({'action': 'record_result', 'at': now(), 'receipt': ref})
        # Research rounds and reflection require an explicit reviewed question,
        # not an automatic +1 for each account or rewritten result receipt.
    update_state(root, expected_revision, change)


def change_execution(root, action, expected_revision, reason=None, receipt=None, line=None):
    require(action in ('pause', 'resume'), 'invalid_execution_action')
    def change(current):
        execution = execution_for(current, line)
        if action == 'pause':
            require(isinstance(reason, str) and bool(reason.strip()), 'pause_reason_required')
            execution.update(status='paused', pause_reason=reason)
        else:
            path = inside(root, str(receipt))
            ref = {'path': relative(root, path), 'sha256': sha(path)}
            proposed = dict(execution, status='active', authorization=ref)
            if line is None:
                provisional = {'execution': proposed}
            else:
                provisional = {**current, 'execution_by_line': {**current['execution_by_line'], line: proposed}}
            authorization(root, provisional, line)
            require(not any(e.get('authorization_sha256') == ref['sha256'] for e in execution['events']),
                    'resume_receipt_already_used')
            execution.update(status='active', authorization=ref, pause_reason=None)
        execution['events'].append({'action': action, 'at': now(), 'reason': reason,
                                    'authorization_sha256': (execution.get('authorization') or {}).get('sha256')})
    update_state(root, expected_revision, change)


def reconcile(root, expected_revision, receipt_path):
    path = inside(root, str(receipt_path))
    ref = {'path': relative(root, path), 'sha256': sha(path)}
    receipt = read(bound(root, ref))
    require(isinstance(receipt, dict) and receipt.get('schema_version') == 'research_reconciliation.v1',
            'invalid_reconciliation_receipt')
    outcome = receipt.get('outcome')
    require(outcome in ('completed', 'failed', 'confirmed_not_submitted'), 'unknown_is_not_terminal')
    require(isinstance(receipt.get('reason'), str) and bool(receipt['reason'].strip()), 'reconciliation_reason_required')
    evidence = receipt.get('evidence')
    require(isinstance(evidence, list) and bool(evidence), 'reconciliation_evidence_required')
    for item in evidence:
        bound(root, item)
    require(outcome == 'confirmed_not_submitted' or bool(receipt.get('backtest_id')), 'platform_identity_required')
    def change(current):
        _line, row = attempt_owner(current, 'id', receipt.get('attempt_id'))
        require(row['plan_sha256'] == receipt.get('plan_sha256'), 'wrong_reconciliation_identity')
        require(row['status'] not in ('completed', 'failed', 'confirmed_not_submitted'), 'attempt_already_reconciled')
        row.update(status=outcome, reconciliation=ref, reconciled_at=now())
        # Neither a completion nor an absence proof resumes research or refunds budget.
    update_state(root, expected_revision, change)


def status_view(root, data, line=None, shared=False):
    """A bounded disclosure for the requested line, without other agents' logs."""
    current = data['current']
    if data['control_schema'] == SCHEMA:
        require(line is None, 'line_isolation_migration_required')
        return {'revision': data['control_revision'], 'current': current}
    selected = [line] if line is not None else list(LINES)
    summaries = {}
    for owner in selected:
        execution = execution_for(current, owner)
        ref = execution.get('authorization')
        grant_summary = {'recorded': ref is not None, 'active_and_current': False}
        if ref is not None:
            grant_summary['path'] = ref['path']
            try:
                grant = read(bound(root, ref))
                require(isinstance(grant, dict), 'invalid_resume_receipt')
                grant_summary.update({key: grant.get(key) for key in
                                      ('scope', 'workspace', 'expires_at', 'max_submissions', 'max_inflight')})
                authorization(root, current, owner)
                grant_summary['active_and_current'] = True
            except ControlError as exc:
                grant_summary['reason'] = str(exc)
        pending = [a for a in execution['attempts'] if a['status'] not in TERMINAL]
        summaries[owner] = {
            'status': execution['status'], 'pause_reason': execution.get('pause_reason'),
            'applies_to': 'new_submissions_for_this_line_only', 'authorization': grant_summary,
            'recorded_attempt_count': len(execution['attempts']), 'recorded_inflight_count': len(pending),
            'inflight': [{k: a.get(k) for k in ('id', 'experiment_id', 'status', 'slot')} for a in pending],
        }
    result = {'revision': data['control_revision'], 'observed_at': now(),
              'line': line, 'execution_by_line': summaries, 'platform_live_checked': False}
    if line == 'main_strategy':
        result['research'] = {key: current['research'].get(key) for key in
                              ('status', 'latest_experiment', 'latest_report', 'completed_rounds',
                               'rounds_since_reflection', 'independent_validation', 'next_action')}
    if shared:
        result['shared_resources'] = {
            'max_inflight': current['shared_resources']['max_inflight'],
            'recorded_inflight_count': sum(a['status'] not in TERMINAL for a in all_attempts(current)),
            'applies_to': 'new_submissions_only_not_research_or_status_queries',
        }
    return result


def migrate_lines(root, expected_revision, legacy_owner, max_inflight, backup_ref, limit_ref):
    """One-time, offline migration of a reviewed, paused and reconciled v1 ledger.

    Does not issue, renew or activate authorization. Exact historical objects
    survive in their owner's ledger and the hash-bound pre-migration backup.
    """
    require(legacy_owner in LINES, 'invalid_legacy_owner')
    require(type(max_inflight) is int and max_inflight > 0, 'invalid_shared_concurrency_limit')
    backup = read(bound(root, backup_ref))
    limit = read(bound(root, limit_ref))
    require(limit.get('max_inflight') == max_inflight, 'shared_limit_evidence_mismatch')
    with _lock(root):
        original_sha = sha(inside(root, MASTER))
        data = load_state(root)
        require(data['control_schema'] == SCHEMA, 'already_migrated')
        require(data['control_revision'] == expected_revision and data == backup,
                'state_changed_since_migration_review')
        current = data['current']
        legacy = current['execution']
        require(legacy['status'] == 'paused' and all(a['status'] in TERMINAL for a in legacy['attempts']),
                'migration_requires_paused_reconciled_ledger')
        plans = {}
        for row in legacy['attempts']:
            bound(root, {'path': row['plan_path'], 'sha256': row['plan_sha256']})
            plans[row['plan_sha256']] = legacy_owner
        grants = {}
        if legacy.get('authorization') is not None:
            bound(root, legacy['authorization'])
            grants[legacy['authorization']['sha256']] = legacy_owner
        current['execution_by_line'] = {
            owner: {'status': 'paused', 'pause_reason': 'line_submission_receipt_not_registered',
                    'authorization': None, 'attempts': [], 'events': [], 'native_observation': None}
            for owner in LINES
        }
        current['execution_by_line'][legacy_owner] = current.pop('execution')
        current['legacy_line_bindings'] = {'plans': plans, 'authorizations': grants}
        current['shared_resources'] = {'max_inflight': max_inflight, 'evidence': limit_ref,
                                       'scope': 'aggregate_new_submissions_only'}
        current['line_migration'] = {'at': now(), 'legacy_owner': legacy_owner, 'backup': backup_ref,
                                      'authorization_resumed': False}
        data['control_schema'] = SCOPED_SCHEMA
        data['control_revision'] += 1
        current['updated_at'] = now()
        _save(root, data, original_sha)
    return {'migrated': True, 'revision': data['control_revision'], 'submitted': False, 'resumed': False}


def launch_command(root, info):
    run = info['run']
    return [sys.executable, '-X', 'utf8', '-B', str(Path(root) / 'engine/jq_backtest.py'),
            str(info['source']), run['start'], run['end'], '--full', '--' + run['frequency'], '--no-diagnosis',
            '--skip-log', '--skip-attribution', '--new-slot', '--slot', run['slot'],
            '--output-dir', str(info['output']), '--research-plan', str(info['plan_path'])]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='action', required=True)
    status_parser = commands.add_parser('status')
    status_parser.add_argument('--line', choices=LINES)
    status_parser.add_argument('--shared', action='store_true')
    for name in ('check', 'run'):
        p = commands.add_parser(name)
        p.add_argument('--plan', required=True)
    for name in ('pause', 'resume', 'reconcile', 'record-result'):
        p = commands.add_parser(name)
        p.add_argument('--expected-revision', type=int, required=True)
        p.add_argument('--reason' if name == 'pause' else '--receipt', required=True)
        if name in ('pause', 'resume'):
            p.add_argument('--line', choices=LINES)
    args = parser.parse_args(argv)
    try:
        if args.action == 'status':
            data = load_state(ROOT)
            print(json.dumps(status_view(ROOT, data, args.line, args.shared), ensure_ascii=False, indent=2))
        elif args.action in ('pause', 'resume'):
            change_execution(ROOT, args.action, args.expected_revision,
                              reason=getattr(args, 'reason', None), receipt=getattr(args, 'receipt', None), line=args.line)
            print(json.dumps({'updated': True, 'action': args.action, 'submitted': False}))
        elif args.action == 'record-result':
            record_result(ROOT, args.expected_revision, args.receipt)
            print(json.dumps({'recorded': True, 'submitted': False, 'auto_promoted': False}))
        elif args.action == 'reconcile':
            reconcile(ROOT, args.expected_revision, args.receipt)
            print(json.dumps({'reconciled': True, 'submitted': False}))
        else:
            info = preflight(ROOT, args.plan)
            if args.action == 'check':
                print(json.dumps({'control_pass': True, 'submitted': False,
                                  'identity': info['identity'], 'economic_adoption': False}))
            else:
                env = dict(os.environ, **safe_environment(info), PYTHONUTF8='1', PYTHONDONTWRITEBYTECODE='1')
                return subprocess.run(launch_command(ROOT, info), cwd=ROOT, env=env).returncode
        return 0
    except (ControlError, OSError) as exc:
        print(json.dumps({'control_pass': False, 'submitted': False, 'reason': str(exc)}, ensure_ascii=False))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
