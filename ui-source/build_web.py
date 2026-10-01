#!/usr/bin/env python3
"""
生成独立的单文件 HTML 网页版托福词汇背诵助手（企业级防丢失、高健壮性）
- 构建前自动从“进度最新”的数据库导出内置快照，杜绝新版网页进度回退
- 双重持久化存储：LocalStorage + IndexedDB + 每日快照自动容灾
- 新版网页启动时会自动把内置最新快照智能合并进浏览器本地数据（只补不删）
- 全链路 XSS 与 HTML 标签防吞字转义 (esc)
- 纯双引擎发音：Web Speech API + 有道极速音频流兜底
- 全键盘盲操 + 移动端自适应
"""
import json
import argparse
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
os.chdir(BASE_DIR)  # 无论从哪个目录调用本脚本，资源路径都保持一致
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

parser = argparse.ArgumentParser(description='生成本地或云端词汇网页')
parser.add_argument('--cloud', action='store_true')
parser.add_argument('--output')
BUILD_ARGS = parser.parse_args()
SNAPSHOT_PATH = BASE_DIR / 'data_export_for_web.json' 


def _candidate_databases():
    """收集所有可能保存最新学习进度的数据库位置（按优先级排列）"""
    paths = []
    env_db = os.environ.get('TOEFL_VOCAB_DB')
    if env_db:
        paths.append(Path(env_db).expanduser())
    paths.append(BASE_DIR / 'data' / 'vocabulary.db')  # 开发/源码运行
    # macOS 打包版默认数据目录
    paths.append(Path.home() / 'Library' / 'Application Support' / 'TOEFLVocab' / 'vocabulary.db')
    return paths


def _db_fingerprint(db_path: Path):
    """读取数据库指纹：(最后复习时间, 词数, 复习记录数)"""
    try:
        conn = sqlite3.connect(f'file:{db_path}?mode=ro', uri=True)
    except Exception:
        conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.cursor()
        cur.execute('SELECT COUNT(*) FROM words')
        words = cur.fetchone()[0] or 0
        cur.execute('SELECT COUNT(*) FROM review_logs')
        logs = cur.fetchone()[0] or 0
        cur.execute('SELECT MAX(reviewed_at) FROM review_logs')
        last_review = cur.fetchone()[0] or ''
    finally:
        conn.close()
    return str(last_review), int(words), int(logs)


def pick_latest_database():
    """挑选“进度最新、数据最全”的数据库，避免内置快照落后于真实进度"""
    best_path, best_key, best_stat = None, None, None
    for p in _candidate_databases():
        try:
            if not p.exists() or p.stat().st_size == 0:
                continue
            last_review, words, logs = _db_fingerprint(p)
        except Exception as e:
            print(f'⚠️  跳过无法读取的数据库 {p}：{e}')
            continue
        key = (last_review, words, logs)
        if best_key is None or key > best_key:
            best_path, best_key, best_stat = p, key, (words, logs)
    return best_path, best_stat


def refresh_snapshot():
    """构建前用最新进度重写 data_export_for_web.json（失败则沿用旧快照并告警）"""
    db_path, stat = pick_latest_database()
    if not db_path:
        print('⚠️  未找到可用数据库，继续使用现有 data_export_for_web.json 快照。')
        return
    try:
        from src.core import database as db_module
        from src.utils import sync_manager

        # 指向选中的数据库，复用客户端同款导出逻辑，保证格式 100% 一致
        db_module.DATA_DIR = db_path.parent
        db_module.DB_PATH = db_path
        db_module.init_db()
        payload = sync_manager.export_sync_data()
        with open(SNAPSHOT_PATH, 'w', encoding='utf-8') as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        summary = payload.get('summary', {}) or {}
        print('📸 已刷新网页内置进度快照')
        print(f'   • 数据来源：{db_path}')
        print(f'   • 词单 {summary.get("total_lists", 0)} 个 / 单词 {summary.get("total_words", 0)} 个 / 复习记录 {summary.get("total_review_logs", 0)} 条')
        print(f'   • 导出时间：{payload.get("exported_at")}')
    except Exception as e:
        print(f'⚠️  自动导出快照失败（{e}），继续使用现有 data_export_for_web.json 快照。')


if BUILD_ARGS.cloud:
    default_data_json = json.dumps({'format':'toefl_vocab_sync','version':4,'words':[],'word_lists':[], 'photo_state':{}})
else:
    refresh_snapshot()
    with open(SNAPSHOT_PATH, 'r', encoding='utf-8') as f:
        default_data_json = f.read()

_snapshot = json.loads(default_data_json)
_snapshot_summary = _snapshot.get('summary') or {}
snapshot_meta_json = json.dumps({
    'exported_at': _snapshot.get('exported_at', ''),
    'total_words': _snapshot_summary.get('total_words', len(_snapshot.get('words') or [])),
    'total_review_logs': _snapshot_summary.get('total_review_logs', 0),
    'built_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
}, ensure_ascii=False)
BUILD_STAMP = datetime.now().strftime('%Y-%m-%d %H:%M')

# 说明：下面这段 JS 以普通字符串注入页面，因此内部可以放心使用单层大括号
WEB_STATE_MERGE_JS = r'''
    // ══════════════════════════════════════════════════════════════════
    //  🧩 统一进度合并内核
    //  本地数据 × 网页内置快照 × 云端/导入数据包，全部走同一套合并规则，
    //  保证“多端 + 多次更新网页”之后进度始终是同一份最新状态。
    // ══════════════════════════════════════════════════════════════════
    let pendingBuiltinMergeStats = null;

    // 已删除单词墓碑：避免新版内置快照把用户主动删掉的词又合并回来
    function getDeletedWordKeys() {
      try {
        const raw = localStorage.getItem(DELETED_WORDS_KEY);
        const arr = raw ? JSON.parse(raw) : [];
        return new Set(Array.isArray(arr) ? arr.filter(Boolean) : []);
      } catch (e) {
        return new Set();
      }
    }

    function rememberDeletedWord(word) {
      const key = String(word || '').trim().toLowerCase();
      if (!key) return;
      try {
        const set = getDeletedWordKeys();
        set.add(key);
        localStorage.setItem(DELETED_WORDS_KEY, JSON.stringify(Array.from(set)));
      } catch (e) {}
    }

    function clearDeletedWordKeys() {
      try { localStorage.removeItem(DELETED_WORDS_KEY); } catch (e) {}
    }

    // 统一复习计划字段（保留未知扩展字段，缺省值补齐）
    function normalizeSchedule(s) {
      if (!s) return null;
      const out = Object.assign({}, s);
      out.stability = s.stability || 0;
      out.difficulty = s.difficulty || 0;
      out.elapsed_days = s.elapsed_days || 0;
      out.scheduled_days = s.scheduled_days || 0;
      out.reps = s.reps || 0;
      out.lapses = s.lapses || 0;
      out.state = s.state || 0;
      return out;
    }

    // 外部进度是否比本地更“新”：
    // 复习次数多者优先 → 次数相同比记忆稳定性 → 再相同比到期时间（越晚越说明记得牢）
    // 🛡️ 保护机制：若本地已达到长期掌握状态，外部若未达到掌握标准，绝不可覆盖！
    function isScheduleBetter(incoming, current) {
      if (!incoming) return false;
      if (!current) return true;
      const cMastered = (current.state === 2 && ((current.stability || 0) >= 7.0 || (current.reps || 0) >= 3));
      const iMastered = (incoming.state === 2 && ((incoming.stability || 0) >= 7.0 || (incoming.reps || 0) >= 3));
      if (cMastered && !iMastered) {
        return false;
      }
      const iReps = incoming.reps || 0;
      const cReps = current.reps || 0;
      if (iReps !== cReps) return iReps > cReps;
      const iStab = incoming.stability || 0;
      const cStab = current.stability || 0;
      if (iStab !== cStab) return iStab > cStab;
      return String(incoming.due || '') > String(current.due || '');
    }

    /**
     * 把外部数据包（内置快照 / 云端数据 / 导入文件）智能合并进目标词库。
     * 只做“补充与升级”：新增缺失的单词、采用更靠前的复习进度、补录缺失的复习日志；
     * 绝不删除本地单词，也不会用更旧的进度覆盖本地更新的进度。
     */
    function mergeSyncPayloadInto(target, incoming, options) {
      const opts = options || {};
      const stats = { words_added: 0, schedules_updated: 0, reviews_added: 0 };
      if (!target || !incoming || typeof incoming !== 'object') return stats;

      const skipKeys = (opts.deletedKeys instanceof Set) ? opts.deletedKeys : null;

      if (!Array.isArray(target.word_lists)) target.word_lists = [];
      if (!Array.isArray(target.words)) target.words = [];

      // 1. 词单：同名视为同一个词单，缺失的补进来
      const listMap = new Map();
      target.word_lists.forEach(l => { if (l && l.name) listMap.set(l.name, l); });
      (incoming.word_lists || []).forEach(l => {
        if (!l || !l.name || listMap.has(l.name)) return;
        const maxId = target.word_lists.reduce((m, x) => Math.max(m, Number(x.id) || 0), 0);
        const newL = Object.assign({}, l, { id: maxId + 1 });
        target.word_lists.push(newL);
        listMap.set(newL.name, newL);
      });
      if (target.word_lists.length === 0) {
        const firstIncoming = (incoming.word_lists || [])[0] || {};
        const newL = {
          id: 1,
          name: firstIncoming.name || '📘 托福核心词汇',
          description: firstIncoming.description || '官方核心高频词汇',
          color: firstIncoming.color || '#2563EB',
          created_at: firstIncoming.created_at || new Date().toISOString()
        };
        target.word_lists.push(newL);
        listMap.set(newL.name, newL);
      }

      // 2. 单词：以「小写单词」为唯一键，跨设备 / 跨版本都不会因 ID 不同而错位
      const wordMap = new Map();
      target.words.forEach(w => wordMap.set(String(w.word || '').trim().toLowerCase(), w));

      (incoming.words || []).forEach(item => {
        if (!item) return;
        const key = String(item.word || '').trim().toLowerCase();
        if (!key) return;
        if (skipKeys && skipKeys.has(key)) return;

        const targetList = listMap.get(item.list_name) || target.word_lists[0];

        if (!wordMap.has(key)) {
          const maxWid = target.words.reduce((m, x) => Math.max(m, Number(x.id) || 0), 0);
          const newW = {
            id: Math.max(Date.now(), maxWid + 1),
            word: item.word,
            phonetic: item.phonetic || '',
            meaning: item.meaning || '',
            example: item.example || '',
            note: item.note || '',
            category: item.category || 'plan',
            list_id: targetList ? targetList.id : 1,
            list_name: targetList ? targetList.name : '📘 托福核心词汇',
            created_at: item.created_at || new Date().toISOString(),
            photo_pending: !!item.photo_pending,
            schedule: normalizeSchedule(item.schedule),
            reviews: Array.isArray(item.reviews) ? item.reviews.slice() : []
          };
          target.words.push(newW);
          wordMap.set(key, newW);
          stats.words_added++;
          return;
        }

        const existing = wordMap.get(key);
        ['phonetic', 'meaning', 'example', 'note'].forEach(f => {
          if (!existing[f] && item[f]) existing[f] = item[f];
        });

        if (item.photo_pending === false) existing.photo_pending = false;
        const incSchedule = normalizeSchedule(item.schedule);
        if (incSchedule && isScheduleBetter(incSchedule, existing.schedule)) {
          existing.schedule = incSchedule;
          stats.schedules_updated++;
        }

        if (Array.isArray(item.reviews)) {
          if (!Array.isArray(existing.reviews)) existing.reviews = [];
          const seen = new Set(existing.reviews.map(r => `${r && r.rating}_${r && r.reviewed_at}`));
          item.reviews.forEach(r => {
            if (!r) return;
            const k = `${r.rating}_${r.reviewed_at}`;
            if (seen.has(k)) return;
            existing.reviews.push(r);
            seen.add(k);
            stats.reviews_added++;
          });
        }
      });

      PhotoCore.merge(target, incoming);
      return stats;
    }

    // ── 网页内置进度快照：每次打开都会自动合并一次（只补不删） ──
    function builtinSnapshotStamp() {
      const meta = (typeof BUILTIN_SNAPSHOT_META !== 'undefined') ? BUILTIN_SNAPSHOT_META : null;
      if (meta && meta.exported_at) return String(meta.exported_at);
      if (typeof INITIAL_DEFAULT_DATA !== 'undefined' && INITIAL_DEFAULT_DATA && INITIAL_DEFAULT_DATA.exported_at) {
        return String(INITIAL_DEFAULT_DATA.exported_at);
      }
      return 'unknown';
    }

    function mergeBuiltinSnapshot(force) {
      const snap = (typeof INITIAL_DEFAULT_DATA !== 'undefined') ? INITIAL_DEFAULT_DATA : null;
      if (!snap || !Array.isArray(snap.words) || snap.words.length === 0) return null;
      const stamp = builtinSnapshotStamp();
      try {
        // 同一份内置快照只自动合并一次，避免每次刷新都做无谓的全量比对
        if (!force && localStorage.getItem(BUILTIN_MERGED_KEY) === stamp) return null;
        const stats = mergeSyncPayloadInto(appData, snap, { deletedKeys: getDeletedWordKeys() });
        localStorage.setItem(BUILTIN_MERGED_KEY, stamp);
        return stats;
      } catch (e) {
        console.warn('内置快照合并异常', e);
        return null;
      }
    }

    function builtinMergeStatsText(stats) {
      if (!stats) return '';
      return `新增 ${stats.words_added} 词 / 升级 ${stats.schedules_updated} 条进度 / 补录 ${stats.reviews_added} 条复习记录`;
    }

    function renderBuiltinSnapshotInfo() {
      const el = document.getElementById('builtin-snapshot-info');
      if (!el) return;
      const meta = (typeof BUILTIN_SNAPSHOT_META !== 'undefined') ? (BUILTIN_SNAPSHOT_META || {}) : {};
      const snapTime = String(meta.exported_at || '').replace('T', ' ').slice(0, 19) || '未知';
      let merged = '';
      try { merged = localStorage.getItem(BUILTIN_MERGED_KEY) || ''; } catch (e) {}
      const mergedTime = merged ? merged.replace('T', ' ').slice(0, 19) : '尚未合并';
      el.innerHTML =
        `📦 新版网页内置进度快照：<b>${esc(snapTime)}</b> · ${meta.total_words || 0} 词 / ${meta.total_review_logs || 0} 条复习记录<br>` +
        `🧩 本机已合并该快照：<b>${esc(mergedTime)}</b>（每次打开网页会自动把新版内置的最新进度合并进来，只补不删）`;
    }

    async function mergeBuiltinSnapshotAction() {
      const stats = mergeBuiltinSnapshot(true);
      if (!stats) {
        showToast('⚠️ 内置进度快照不可用');
        return;
      }
      await saveData();
      updateAllListDropdowns();
      renderAllViews();
      renderBuiltinSnapshotInfo();
      showToast(`✅ 已合并内置进度：${builtinMergeStatsText(stats)}`);
    }

    // ── 🧹 自动去重：同一个单词（忽略大小写与首尾空格）只保留一条记录 ──
    //  合并规则：保留最早创建的那条为主记录，补齐缺失字段，
    //  取进度更靠前的复习计划，并把两边的复习历史合并去重。
    function dedupeWords(target) {
      const stats = { duplicates_removed: 0, schedules_upgraded: 0, reviews_added: 0 };
      if (!target || !Array.isArray(target.words) || target.words.length === 0) return stats;

      const byKey = new Map();
      const keptWords = [];

      target.words.forEach(w => {
        if (!w) return;
        const key = String(w.word || '').trim().toLowerCase();
        if (!key) { keptWords.push(w); return; }

        if (!byKey.has(key)) {
          byKey.set(key, w);
          keptWords.push(w);
          return;
        }

        // 命中重复词：并入首条记录
        const kept = byKey.get(key);
        ['phonetic', 'meaning', 'example', 'note'].forEach(f => {
          if (!kept[f] && w[f]) kept[f] = w[f];
        });
        if (!kept.category && w.category) kept.category = w.category;
        if (!kept.created_at && w.created_at) kept.created_at = w.created_at;

        const incomingSchedule = normalizeSchedule(w.schedule);
        if (incomingSchedule && isScheduleBetter(incomingSchedule, kept.schedule)) {
          kept.schedule = incomingSchedule;
          stats.schedules_upgraded++;
        }

        if (Array.isArray(w.reviews) && w.reviews.length > 0) {
          if (!Array.isArray(kept.reviews)) kept.reviews = [];
          const seen = new Set(kept.reviews.map(r => `${r && r.rating}_${r && r.reviewed_at}`));
          w.reviews.forEach(r => {
            if (!r) return;
            const k = `${r.rating}_${r.reviewed_at}`;
            if (seen.has(k)) return;
            kept.reviews.push(r);
            seen.add(k);
            stats.reviews_added++;
          });
        }

        stats.duplicates_removed++;
      });

      if (stats.duplicates_removed > 0) target.words = keptWords;
      return stats;
    }

    function countDuplicateWords(target) {
      if (!target || !Array.isArray(target.words)) return 0;
      const seen = new Set();
      let dup = 0;
      target.words.forEach(w => {
        const key = String((w && w.word) || '').trim().toLowerCase();
        if (!key) return;
        if (seen.has(key)) { dup++; return; }
        seen.add(key);
      });
      return dup;
    }

    function dedupeStatsText(stats) {
      if (!stats || !stats.duplicates_removed) return '';
      return `清理重复词 ${stats.duplicates_removed} 条 / 合并进度 ${stats.schedules_upgraded} 条 / 合并复习记录 ${stats.reviews_added} 条`;
    }

    async function dedupeWordsAction(silent) {
      const stats = dedupeWords(appData);
      if (!stats.duplicates_removed) {
        if (!silent) showToast('✅ 词库很干净，没有发现重复单词');
        renderBuiltinSnapshotInfo();
        return stats;
      }
      await saveData();
      updateAllListDropdowns();
      renderAllViews();
      renderBuiltinSnapshotInfo();
      showToast(`🧹 自动去重完成：${dedupeStatsText(stats)}`);
      return stats;
    }

    // 统一刷新所有视图（同步 / 导入 / 去重后调用，避免界面还停在旧数据）
    function renderAllViews() {
      try { updateGlobalStatusBar(); } catch (e) {}
      try { updateAllListDropdowns(); } catch (e) {}
      try { renderWordListTable(); } catch (e) {}
      try { renderStatsView(); } catch (e) {}
      if (document.getElementById("view-photo")?.classList.contains("active")) { try { PhotoUI.render(); } catch (e) {} }
      const reviewPage = document.getElementById('view-review');
      if (reviewPage && reviewPage.classList.contains('active')) {
        try { loadReviewQueue(); } catch (e) {}
      }
    }

    // 构建时间戳：方便一眼确认自己用的是哪一版网页（排查"改了没生效"类问题）
    function renderBuildStamp() {
      const stamp = (typeof APP_BUILD_STAMP !== 'undefined') ? APP_BUILD_STAMP : '未知';
      const tip = '网页版构建于 ' + stamp;
      const side = document.getElementById('app-build-stamp');
      if (side) side.innerHTML = '🕒 构建 ' + esc(stamp) + '<br>Excel/Word/JSON 导入版';
      const inModal = document.getElementById('import-build-stamp');
      if (inModal) inModal.textContent = stamp;
    }
'''

# 说明：Excel(.xlsx) / Word(.docx) / CSV 直接导入（纯前端零依赖解析）
# 这段同样是普通字符串注入，内部可放心使用单层大括号
WEB_IMPORT_JS = r'''
    // ══════════════════════════════════════════════════════════════════
    //  📥 Excel / Word / CSV 直接导入（不依赖任何第三方库、不需要联网）
    // ══════════════════════════════════════════════════════════════════
    let pendingImport = null;   // { rows, mapping, headerRow, fileName, parsed }

    const IMPORT_FIELD_LABELS = {
      ignore: '🚫 忽略', word: '🔤 单词', phonetic: '🔉 音标',
      meaning: '🈶 释义', example: '📝 例句', note: '🏷 备注'
    };
    const IMPORT_FIELDS = ['word', 'phonetic', 'meaning', 'example', 'note'];
    const IMPORT_HEADER_HINTS = {
      word: ['单词', '词汇', '英文', 'word', 'words', 'spelling', 'term'],
      phonetic: ['音标', '发音', 'phonetic', 'pronunciation', 'ipa'],
      meaning: ['释义', '含义', '意思', '中文', '词义', '翻译', '解释', 'meaning', 'definition', 'translation'],
      example: ['例句', '例子', '原句', '真题', 'example', 'sentence'],
      note: ['备注', '笔记', '出处', '考点', '标签', '派生', '分类', '词单', 'note', 'remark', 'tag']
    };
    const IMPORT_NOISE_PATTERNS = [/单词总数/, /词汇总数/, /错误数/, /正确数/, /正确率/, /^word\s*list/i, /^list\s*\d+/i];
    const POS_ABBR = /^(?:adj|adv|n|v|vt|vi|prep|conj|pron|num|art|int|aux|abbr|pl|sb|sth)\.?$/i;

    // ── 通用小工具 ──
    function fileExt(name) {
      const m = String(name || '').toLowerCase().match(/\.([a-z0-9]+)$/);
      return m ? m[1] : '';
    }

    async function drainStreamToText(stream) {
      const reader = stream.getReader();
      const decoder = new TextDecoder('utf-8');
      let out = '';
      while (true) {
        const step = await reader.read();
        if (step.done) break;
        out += decoder.decode(step.value, { stream: true });
      }
      out += decoder.decode();
      return out;
    }

    function parseXml(text) {
      const doc = new DOMParser().parseFromString(text, 'application/xml');
      if (doc.getElementsByTagName('parsererror').length) throw new Error('文件内容已损坏，无法解析');
      return doc;
    }

    function attrByLocalName(el, localName) {
      if (!el || !el.attributes) return null;
      for (let i = 0; i < el.attributes.length; i++) {
        const a = el.attributes[i];
        if (a.localName === localName || a.name === localName) return a.value;
      }
      return null;
    }

    // ── ZIP 解包（xlsx / docx 本质都是 zip） ──
    async function unzipToEntryMap(buffer) {
      const view = new DataView(buffer);
      const bytes = new Uint8Array(buffer);
      let eocd = -1;
      const minPos = Math.max(0, buffer.byteLength - 66000);
      for (let i = buffer.byteLength - 22; i >= minPos; i--) {
        if (view.getUint32(i, true) === 0x06054b50) { eocd = i; break; }
      }
      if (eocd < 0) throw new Error('文件结构不是有效的 xlsx / docx');

      const totalEntries = view.getUint16(eocd + 10, true);
      let offset = view.getUint32(eocd + 16, true);
      const decoder = new TextDecoder('utf-8');
      const map = {};

      for (let i = 0; i < totalEntries; i++) {
        if (offset + 46 > bytes.length || view.getUint32(offset, true) !== 0x02014b50) break;
        const method = view.getUint16(offset + 10, true);
        const compSize = view.getUint32(offset + 20, true);
        const nameLen = view.getUint16(offset + 28, true);
        const extraLen = view.getUint16(offset + 30, true);
        const commentLen = view.getUint16(offset + 32, true);
        const localOffset = view.getUint32(offset + 42, true);
        const name = decoder.decode(bytes.subarray(offset + 46, offset + 46 + nameLen));
        const localNameLen = view.getUint16(localOffset + 26, true);
        const localExtraLen = view.getUint16(localOffset + 28, true);
        const dataStart = localOffset + 30 + localNameLen + localExtraLen;
        map[name] = { method, data: bytes.subarray(dataStart, dataStart + compSize) };
        offset += 46 + nameLen + extraLen + commentLen;
      }
      return map;
    }

    async function readZipEntryText(map, name) {
      const entry = map[name];
      if (!entry) return null;
      if (entry.method === 0) return new TextDecoder('utf-8').decode(entry.data);
      if (typeof DecompressionStream === 'undefined') {
        throw new Error('当前浏览器版本过旧，无法解压。请在 Excel 里另存为 CSV 后再导入');
      }
      const stream = new Blob([entry.data]).stream().pipeThrough(new DecompressionStream('deflate-raw'));
      return await drainStreamToText(stream);
    }

    // ── Excel(.xlsx) → 二维行数组 ──
    function xlsxSharedStrings(doc) {
      const list = [];
      const items = doc.getElementsByTagName('si');
      for (let i = 0; i < items.length; i++) {
        let text = '';
        for (const child of Array.from(items[i].children)) {
          if (child.localName === 't') text += child.textContent || '';
          else if (child.localName === 'r') {
            for (const t of Array.from(child.children)) {
              if (t.localName === 't') text += t.textContent || '';
            }
          }
        }
        list.push(text);
      }
      return list;
    }

    function xlsxColIndex(ref) {
      const m = String(ref || '').match(/^([A-Za-z]+)/);
      if (!m) return -1;
      const letters = m[1].toUpperCase();
      let n = 0;
      for (let i = 0; i < letters.length; i++) n = n * 26 + (letters.charCodeAt(i) - 64);
      return n - 1;
    }

    function xlsxRowToCells(rowEl, shared) {
      const cells = [];
      for (const c of Array.from(rowEl.children)) {
        if (c.localName !== 'c') continue;
        const idx = xlsxColIndex(c.getAttribute('r'));
        const type = c.getAttribute('t') || '';
        let value = '';
        if (type === 'inlineStr') {
          const ts = c.getElementsByTagName('t');
          for (let i = 0; i < ts.length; i++) value += ts[i].textContent || '';
        } else {
          let v = null;
          for (const child of Array.from(c.children)) {
            if (child.localName === 'v') { v = child; break; }
          }
          if (v) {
            const raw = v.textContent || '';
            if (type === 's') {
              const si = parseInt(raw, 10);
              value = (shared && shared[si] != null) ? shared[si] : '';
            } else {
              value = raw;
            }
          }
        }
        const at = idx >= 0 ? idx : cells.length;
        while (cells.length < at) cells.push('');
        cells[at] = value;
      }
      return cells;
    }

    function walkXlsxRows(el, rows, shared) {
      for (const child of Array.from(el.children || [])) {
        if (child.localName === 'row') {
          const cells = xlsxRowToCells(child, shared);
          if (cells.some(v => String(v).trim() !== '')) rows.push(cells);
        } else {
          walkXlsxRows(child, rows, shared);
        }
      }
    }

    async function parseXlsxRows(buffer) {
      const map = await unzipToEntryMap(buffer);

      const sharedXml = await readZipEntryText(map, 'xl/sharedStrings.xml');
      const shared = sharedXml ? xlsxSharedStrings(parseXml(sharedXml)) : [];

      const relXml = await readZipEntryText(map, 'xl/_rels/workbook.xml.rels');
      const relMap = {};
      if (relXml) {
        const rels = parseXml(relXml).getElementsByTagName('Relationship');
        for (let i = 0; i < rels.length; i++) relMap[rels[i].getAttribute('Id')] = rels[i].getAttribute('Target');
      }

      const sheetPaths = [];
      const wbXml = await readZipEntryText(map, 'xl/workbook.xml');
      if (wbXml) {
        const sheets = parseXml(wbXml).getElementsByTagName('sheet');
        for (let i = 0; i < sheets.length; i++) {
          const rid = attrByLocalName(sheets[i], 'id');
          let target = rid ? relMap[rid] : null;
          if (!target) continue;
          if (target.charAt(0) === '/') target = target.slice(1);
          else if (target.indexOf('xl/') !== 0) target = 'xl/' + target.replace(/^\.\//, '');
          if (map[target]) sheetPaths.push(target);
        }
      }
      if (!sheetPaths.length) {
        Object.keys(map).filter(n => /^xl\/worksheets\/[^\/]+\.xml$/.test(n)).sort().forEach(n => sheetPaths.push(n));
      }

      const rows = [];
      for (const path of sheetPaths) {
        const xml = await readZipEntryText(map, path);
        if (!xml) continue;
        walkXlsxRows(parseXml(xml), rows, shared);
      }
      return rows;
    }

    // ── Word(.docx) → 二维行数组（表格按行、单元格分列；段落按单列行） ──
    function docxCollectText(el, out) {
      for (const child of Array.from(el.children || [])) {
        const name = child.localName;
        if (name === 't') out.push(child.textContent || '');
        else if (name === 'tab') out.push(' ');
        else if (name === 'br' || name === 'cr') out.push('\n');
        else if (name === 'rPh') continue;
        else docxCollectText(child, out);
      }
    }

    function docxText(el) {
      const out = [];
      docxCollectText(el, out);
      return out.join('');
    }

    function docxCellText(tc) {
      const parts = [];
      for (const el of Array.from(tc.getElementsByTagName('*'))) {
        if (el.localName === 'p') {
          const t = docxText(el).replace(/\s+/g, ' ').trim();
          if (t) parts.push(t);
        }
      }
      return parts.join(' ').trim();
    }

    async function parseDocxRows(buffer) {
      const map = await unzipToEntryMap(buffer);
      const xml = await readZipEntryText(map, 'word/document.xml');
      if (!xml) throw new Error('未找到 Word 正文（word/document.xml），文件可能已损坏');
      const doc = parseXml(xml);
      const rows = [];

      const walkBlocks = (node) => {
        for (const child of Array.from(node.children || [])) {
          const name = child.localName;
          if (name === 'p') {
            const text = docxText(child).trim();
            if (text) rows.push([text]);
          } else if (name === 'tbl') {
            for (const tr of Array.from(child.children)) {
              if (tr.localName !== 'tr') continue;
              const cells = [];
              for (const tc of Array.from(tr.children)) {
                if (tc.localName !== 'tc') continue;
                cells.push(docxCellText(tc));
              }
              if (cells.some(c => c !== '')) rows.push(cells);
            }
          } else {
            walkBlocks(child);
          }
        }
      };
      walkBlocks(doc.documentElement);
      return rows;
    }

    // ── CSV / TSV / 纯文本 → 二维行数组 ──
    function splitDelimitedLine(line, delim) {
      const out = [];
      let cur = '';
      let quoted = false;
      for (let i = 0; i < line.length; i++) {
        const ch = line[i];
        if (quoted) {
          if (ch === '"') {
            if (line[i + 1] === '"') { cur += '"'; i++; }
            else quoted = false;
          } else cur += ch;
        } else if (ch === '"') {
          quoted = true;
        } else if (ch === delim) {
          out.push(cur); cur = '';
        } else {
          cur += ch;
        }
      }
      out.push(cur);
      return out.map(s => s.trim());
    }

    function parseDelimitedRows(text) {
      const raw = String(text || '').replace(/^\uFEFF/, '');
      const lines = raw.split(/\r\n|\r|\n/);
      const sample = lines.slice(0, 10).join('\n');
      let delim = '\t';
      if (sample.indexOf('\t') < 0) {
        if (sample.indexOf(',') >= 0) delim = ',';
        else if (sample.indexOf('，') >= 0) delim = '，';
        else if (sample.indexOf(';') >= 0) delim = ';';
      }
      return lines.map(line => splitDelimitedLine(line, delim))
                  .filter(parts => parts.some(p => p !== ''));
    }

    // ── 列识别 ──
    function guessMapping(rows) {
      const cellText = (r, c) => String(r[c] == null ? '' : r[c]).trim();
      // 统计只针对真正的数据行，避免被「单词总数：xxx」「序号列」这类行带偏
      const dataRows = rows.filter(r => !isNoiseRow(r.map((v, i) => cellText(r, i))));
      if (!dataRows.length) return {};
      const width = dataRows.reduce((m, r) => Math.max(m, r.length), 0);
      const total = Math.max(dataRows.length, 1);
      const stats = [];
      for (let c = 0; c < width; c++) {
        let filled = 0, latin = 0, cjk = 0, numeric = 0, single = 0, ipa = 0, lenSum = 0, mixed = 0, sentence = 0;
        dataRows.forEach(r => {
          const v = cellText(r, c);
          if (!v) return;
          filled++;
          lenSum += v.length;
          if (/^[\d\s.,%+\-\/年月日]+$/.test(v)) numeric++;
          if (/[A-Za-z]/.test(v)) latin++;
          if (/[\u4e00-\u9fff]/.test(v)) cjk++;
          if (/[A-Za-z]/.test(v) && /[\u4e00-\u9fff]/.test(v)) mixed++;
          if (/^[A-Za-z][A-Za-z'\-]*$/.test(v)) single++;
          if (/[\/\u02c8\u02cc\u0259\u026a\u028a\u03b8\u00f0\u0283\u0292\u014b]/.test(v)) ipa++;
          if (/[.!?。！？]/.test(v)) sentence++;
        });
        stats.push({ index: c, filled, latin, cjk, numeric, single, ipa, mixed, sentence, avgLen: filled ? lenSum / filled : 0 });
      }

      const mapping = {};
      const used = new Set();
      const pick = (ok, score) => stats
        .filter(s => s.filled > 0 && !used.has(s.index) && ok(s))
        .sort((a, b) => score(b) - score(a))[0];

      // 单词列：既接受「纯英文单词列」，也接受「释义+单词写在同一格」的混合列
      const wordStat = pick(
        s => s.numeric < s.filled * 0.6 && (s.cjk < s.filled * 0.5 || s.mixed > s.filled * 0.4),
        s => (s.filled / total) * 0.3 + (s.single / s.filled) * 0.3 + (s.latin / s.filled) * 0.2 + (s.mixed / s.filled) * 0.2);
      if (wordStat) { mapping.word = wordStat.index; used.add(wordStat.index); }

      const ipaStat = pick(s => s.ipa > s.filled * 0.5, s => s.ipa);
      if (ipaStat) { mapping.phonetic = ipaStat.index; used.add(ipaStat.index); }

      const cjkStat = pick(s => s.cjk > s.filled * 0.5, s => s.cjk);
      if (cjkStat) { mapping.meaning = cjkStat.index; used.add(cjkStat.index); }

      // 剩余列：像句子（带句末标点或很长）→ 例句，否则 → 备注（序号/纯数字列直接忽略）
      stats.filter(s => s.filled > 0 && !used.has(s.index) && s.numeric < s.filled * 0.8)
        .sort((a, b) => a.index - b.index)
        .forEach(s => {
          const isSentence = s.sentence > s.filled * 0.3 || s.avgLen > 45;
          if (isSentence && mapping.example === undefined) mapping.example = s.index;
          else if (mapping.note === undefined) mapping.note = s.index;
          else if (mapping.example === undefined) mapping.example = s.index;
        });
      return mapping;
    }

    function detectColumns(rows) {
      let best = null;
      for (let i = 0; i < Math.min(rows.length, 6); i++) {
        const mapping = {};
        let score = 0;
        rows[i].forEach((cell, idx) => {
          const text = String(cell == null ? '' : cell).trim().toLowerCase();
          if (!text) return;
          for (const field of IMPORT_FIELDS) {
            if (mapping[field] !== undefined) continue;
            if (IMPORT_HEADER_HINTS[field].some(h => text.indexOf(h.toLowerCase()) >= 0)) {
              mapping[field] = idx;
              score++;
              break;
            }
          }
        });
        if (mapping.word === undefined) score = 0;
        if (score >= 2 && (!best || score > best.score)) best = { headerRow: i, mapping, score };
      }
      if (best) return best;
      return { headerRow: -1, mapping: guessMapping(rows), score: 0 };
    }

    // ── 单元格内容清洗 ──
    function normalizeWord(raw) {
      let w = String(raw == null ? '' : raw).trim();
      if (!w) return '';
      w = w.replace(/^[\s\u3000]*(?:\d+[\.、\)）:：]|[-•*·※])[\s\u3000]*/, '');
      w = w.replace(/^["'“”‘’]+|["'“”‘’]+$/g, '').trim();
      w = w.replace(/[（(][^（()）]*[\u4e00-\u9fff][^（()）]*[)）]/g, ' ');
      w = w.replace(/\s+/g, ' ').trim();
      return w;
    }

    function splitMixedCell(text) {
      const s = String(text == null ? '' : text).trim();
      if (!s) return null;
      if (!/[\u4e00-\u9fff]/.test(s) || !/[A-Za-z]/.test(s)) return null;
      const cjkParts = s.match(/[\u4e00-\u9fff][\u4e00-\u9fff0-9，。、；：！？（）()《》〈〉“”‘’·\s]*/g) || [];
      const latinParts = s.match(/[A-Za-z][A-Za-z'’\-.\s]*/g) || [];
      let cjk = cjkParts.join(' ').replace(/\s+/g, ' ').trim();
      let latin = latinParts.join(' ').replace(/\s+/g, ' ').replace(/[.\s]+$/, '').trim();
      if (!cjk || !latin) return null;
      const tokens = latin.split(/\s+/).filter(Boolean);
      while (tokens.length > 1 && POS_ABBR.test(tokens[0])) tokens.shift();
      latin = tokens.join(' ').trim();
      if (!/[A-Za-z]{2,}/.test(latin) || POS_ABBR.test(latin)) return null;
      return { word: latin, meaning: cjk };
    }

    function isValidWord(w) {
      if (!w) return false;
      if (!/[A-Za-z]/.test(w)) return false;
      if (/[\u4e00-\u9fff]/.test(w)) return false;
      if (w.length > 60 || w.length < 1) return false;
      if (/^[\d\W]+$/.test(w)) return false;
      return true;
    }

    function isNoiseRow(cells) {
      const joined = cells.join(' ').trim();
      if (!joined) return true;
      if (IMPORT_NOISE_PATTERNS.some(re => re.test(joined))) return true;
      // 纯序号 / 纯数字 / 纯标点行（常见的空白模板列）
      if (cells.every(c => !c || /^[\d\s.,%+\-\/()（）年月日]+$/.test(c))) return true;
      return false;
    }

    function looksLikeHeaderRow(cells) {
      let hits = 0;
      cells.forEach(c => {
        const text = String(c || '').trim().toLowerCase();
        if (!text) return;
        if (IMPORT_FIELDS.some(f => IMPORT_HEADER_HINTS[f].some(h => text.indexOf(h.toLowerCase()) >= 0))) hits++;
      });
      return hits >= 2;
    }

    // ── 二维表格 → 词条（含去重统计与问题行） ──
    function rowsToEntries(rows, mapping, headerRow) {
      const entries = [];
      const problems = [];
      let duplicates = 0;
      const existingKeys = new Set(appData.words.map(w => String(w.word || '').trim().toLowerCase()));
      const batchKeys = new Set();
      const colOf = f => (mapping && mapping[f] !== undefined && mapping[f] >= 0) ? mapping[f] : -1;

      rows.forEach((row, rowIndex) => {
        if (rowIndex === headerRow) return;
        const cells = row.map(v => String(v == null ? '' : v).trim());
        if (isNoiseRow(cells)) return;
        if (looksLikeHeaderRow(cells)) return;
        // 整行没有任何英文字母（例如「x月x日需要加强记忆词汇」这类标题/说明行）→ 静默跳过
        if (!cells.some(c => /[A-Za-z]/.test(c))) return;
        // 「adj. 据称的」这类"词性缩写 + 中文"的残留行 → 静默跳过（不是单词行）
        const firstCell = cells.filter(Boolean)[0] || '';
        if (/[\u4e00-\u9fff]/.test(firstCell)) {
          const head = firstCell.replace(/^\d+[\.、\)）]?\s*/, '').split(/\s+/)[0] || '';
          if (head && POS_ABBR.test(head)) return;
        }

        const pick = f => { const i = colOf(f); return i >= 0 ? (cells[i] || '') : ''; };
        const rawWord = pick('word');
        let word = normalizeWord(rawWord);
        let meaning = pick('meaning');
        let phonetic = pick('phonetic');
        let example = pick('example');
        let note = pick('note');

        // 单个格子里同时写了英文单词和中文释义（如「轻蔑的 dismissive」）
        const mixed = splitMixedCell(rawWord) || (cells.length === 1 ? splitMixedCell(cells[0]) : null);
        if (mixed) {
          word = normalizeWord(mixed.word);
          if (!meaning) meaning = mixed.meaning;
        }

        // 单词列本身没有内容（例如空白模板行、序号行）→ 静默跳过，不计入"无法识别"
        if (!rawWord && !mixed) return;

        if (!isValidWord(word)) {
          // 只有"看着像英文却解析不出单词"的行才提示；纯中文标题/说明行静默跳过
          const suspect = /[A-Za-z]/.test(rawWord) && !/[\u4e00-\u9fff]/.test(rawWord);
          if (suspect) {
            problems.push({ row: rowIndex + 1, reason: '无法识别为英文单词', sample: cells.join(' | ').slice(0, 60) });
          }
          return;
        }

        const key = word.toLowerCase();
        if (existingKeys.has(key) || batchKeys.has(key)) { duplicates++; return; }
        batchKeys.add(key);
        entries.push({
          word: word,
          phonetic: phonetic || '',
          meaning: meaning || '',
          example: example || '',
          note: note || '',
          category: 'plan'
        });
      });

      return { entries: entries, problems: problems, duplicates: duplicates };
    }

    // ── 预览界面 ──
    function renderImportPreview() {
      const area = document.getElementById('import-preview-area');
      const confirmBtn = document.getElementById('import-confirm-btn');
      if (!area) return;
      if (!pendingImport) {
        area.innerHTML = '';
        if (confirmBtn) confirmBtn.disabled = true;
        return;
      }

      const rows = pendingImport.rows;
      const mapping = pendingImport.mapping;
      const width = rows.reduce((m, r) => Math.max(m, r.length), 0);
      const parsed = rowsToEntries(rows, mapping, pendingImport.headerRow);
      pendingImport.parsed = parsed;

      const fieldOptions = (current) => ['ignore'].concat(IMPORT_FIELDS)
        .map(f => `<option value="${f}"${current === f ? ' selected' : ''}>${IMPORT_FIELD_LABELS[f]}</option>`).join('');

      let head = '';
      for (let c = 0; c < width; c++) {
        const current = IMPORT_FIELDS.filter(f => mapping[f] === c)[0] || 'ignore';
        head += `<th style="padding:4px 3px; vertical-align:top;">
          <div style="font-size:0.68rem; color:#94A3B8; margin-bottom:2px;">第 ${c + 1} 列</div>
          <select class="input-field" style="padding:2px 4px; font-size:0.7rem;" onchange="changeImportMapping(${c}, this.value)">${fieldOptions(current)}</select>
        </th>`;
      }

      const body = rows.slice(0, 6).map(r => {
        let tds = '';
        for (let c = 0; c < width; c++) {
          const v = String(r[c] == null ? '' : r[c]).slice(0, 40);
          tds += `<td style="padding:4px 6px; border-top:1px solid rgba(255,255,255,0.07); font-size:0.74rem;">${esc(v)}</td>`;
        }
        return `<tr>${tds}</tr>`;
      }).join('');

      const problemHtml = parsed.problems.length
        ? `<details style="margin-top:8px; font-size:0.75rem; color:#FCA5A5;">
             <summary style="cursor:pointer;">⚠️ ${parsed.problems.length} 行无法识别（点开查看前 10 条）</summary>
             <div style="margin-top:6px; line-height:1.8;">${parsed.problems.slice(0, 10).map(p => `第 ${p.row} 行：${esc(p.reason)} — ${esc(p.sample)}`).join('<br>')}</div>
           </details>`
        : '';

      area.innerHTML = `
        <div style="font-size:0.86rem; color:#CBD5E1; line-height:1.8;">
          📄 <b>${esc(pendingImport.fileName || '粘贴内容')}</b> · 读到 ${rows.length} 行 / ${width} 列<br>
          识别到有效单词 <b style="color:#4ADE80;">${parsed.entries.length}</b> 个
          ${parsed.duplicates ? `· 跳过重复 <b style="color:#FBBF24;">${parsed.duplicates}</b> 个` : ''}
          ${parsed.problems.length ? `· 无法识别 <b style="color:#F87171;">${parsed.problems.length}</b> 行` : ''}
        </div>
        <div style="overflow:auto; max-height:200px; margin-top:8px; border:1px solid rgba(255,255,255,0.09); border-radius:8px;">
          <table style="width:100%; border-collapse:collapse;"><thead>${head}</thead><tbody>${body}</tbody></table>
        </div>
        ${problemHtml}
        <div style="font-size:0.75rem; color:var(--text-muted); margin-top:8px;">
          列识别不对？直接在上方下拉框里改（单词 / 释义 / 音标 / 例句 / 备注），预览会立刻刷新。
        </div>
      `;
      if (confirmBtn) confirmBtn.disabled = parsed.entries.length === 0;
    }

    function changeImportMapping(colIndex, field) {
      if (!pendingImport) return;
      if (field !== 'ignore') {
        IMPORT_FIELDS.forEach(f => { if (f !== field && pendingImport.mapping[f] === colIndex) delete pendingImport.mapping[f]; });
        pendingImport.mapping[field] = colIndex;
      } else {
        IMPORT_FIELDS.forEach(f => { if (pendingImport.mapping[f] === colIndex) delete pendingImport.mapping[f]; });
      }
      renderImportPreview();
    }

    // ── 文件 → 行数组 ──
    async function rowsFromImportFile(file) {
      const ext = fileExt(file.name);
      const buffer = await file.arrayBuffer();
      if (ext === 'xlsx' || ext === 'xlsm') return await parseXlsxRows(buffer);
      if (ext === 'docx') return await parseDocxRows(buffer);
      if (ext === 'csv' || ext === 'tsv' || ext === 'txt') {
        return parseDelimitedRows(new TextDecoder('utf-8').decode(buffer));
      }
      if (ext === 'xls') throw new Error('老版 .xls 是二进制格式，浏览器无法解析：请在 Excel 里「另存为 .xlsx」后再导入（或选中单元格直接复制粘贴到下方的文本框）');
      if (ext === 'doc') throw new Error('老版 .doc 是二进制格式，浏览器无法解析：请在 Word 里「另存为 .docx」后再导入（或直接复制文字粘贴到下方的文本框）');
      const text = new TextDecoder('utf-8').decode(buffer);
      if (text.indexOf('\u0000') >= 0) throw new Error('无法识别的文件格式，请使用 .xlsx / .docx / .csv / .txt');
      return parseDelimitedRows(text);
    }

    function clearImportError() {
      const box = document.getElementById('import-error-box');
      if (box) { box.style.display = 'none'; box.innerHTML = ''; }
    }

    function showImportError(message, detail) {
      const box = document.getElementById('import-error-box');
      const text = String(message == null ? '' : message);
      if (box) {
        box.style.display = 'block';
        box.innerHTML = '❌ ' + esc(text) + (detail ? '<br><span style="color:#94A3B8;">' + esc(String(detail)) + '</span>' : '');
      }
      console.error('[导入失败]', message, detail || '');
      showToast('❌ ' + text, 6000);
    }

    // 同步数据包（.json）走「智能合并」通道，直接并入当前词库
    async function importSyncPackageFile(file) {
      const text = await file.text();
      let json = null;
      try {
        json = JSON.parse(text);
      } catch (e) {
        throw new Error('JSON 内容解析失败，可能不是同步数据包');
      }
      if (!json || (!Array.isArray(json.words) && !Array.isArray(json.word_lists))) {
        throw new Error('这个 JSON 里没有 words 数据，不是本软件的同步数据包');
      }
      const res = await applySyncImportPayload(json, 'merge');
      await saveData();
      updateAllListDropdowns();
      renderAllViews();
      triggerAutoCloudPush();
      pendingImport = null;
      renderImportPreview();
      clearImportError();
      const msg = `✅ 同步数据包导入成功：新增 ${res.words_added} 词，更新 ${res.schedules_updated} 条进度`
        + (res.duplicates_removed ? `，清理重复 ${res.duplicates_removed} 条` : '');
      showToast(msg, 5000);
    }

    async function handleImportFile(file) {
      if (!file) return;
      if (file.size > 30 * 1024 * 1024) { showToast('⚠️ 文件超过 30MB，请精简后再导入', 4000); return; }
      clearImportError();
      showToast('⏳ 正在解析 ' + file.name + ' …', 1500);
      try {
        if (fileExt(file.name) === 'json') {
          await importSyncPackageFile(file);
          return;
        }
        const rows = await rowsFromImportFile(file);
        if (!rows || !rows.length) {
          showImportError('没有从《' + file.name + '》里读到任何内容', '如果文件里是图片或特殊排版，建议改用「复制粘贴文本」方式导入。');
          return;
        }
        const detected = detectColumns(rows);
        pendingImport = { rows: rows, fileName: file.name, headerRow: detected.headerRow, mapping: detected.mapping };
        renderImportPreview();
      } catch (err) {
        showImportError('解析《' + file.name + '》失败：' + (err && err.message ? err.message : err));
      }
    }

    function parsePastedImportText() {
      const box = document.getElementById('import-text-area');
      const text = box ? box.value.trim() : '';
      if (!text) { showToast('请先粘贴要导入的内容'); return; }
      const rows = parseDelimitedRows(text);
      if (!rows.length) { showToast('没有读到任何内容'); return; }
      const detected = detectColumns(rows);
      pendingImport = { rows: rows, fileName: '粘贴的文本', headerRow: detected.headerRow, mapping: detected.mapping };
      renderImportPreview();
    }

    // ── 写入词库（与批量导入共用一套去重规则） ──
    function importWordEntries(entries, targetListId) {
      const existing = new Set(appData.words.map(w => String(w.word || '').trim().toLowerCase()));
      const targetList = appData.word_lists.filter(l => String(l.id) === String(targetListId))[0] || appData.word_lists[0];
      let imported = 0, skipped = 0;
      (entries || []).forEach(e => {
        const key = String(e.word || '').trim().toLowerCase();
        if (!key || existing.has(key)) { skipped++; return; }
        existing.add(key);
        appData.words.push({
          id: generateUniqueId(),
          word: e.word,
          phonetic: e.phonetic || '',
          meaning: e.meaning || '',
          example: e.example || '',
          note: e.note || '',
          category: e.category || 'plan',
          list_id: targetList.id,
          list_name: targetList.name,
          created_at: new Date().toISOString(),
          schedule: {
            due: new Date().toISOString(),
            stability: 0,
            difficulty: 5,
            elapsed_days: 0,
            scheduled_days: 1,
            reps: 0,
            lapses: 0,
            state: 0
          },
          reviews: []
        });
        imported++;
      });
      return { imported: imported, skipped: skipped };
    }

    async function confirmImportEntries() {
      if (!pendingImport || !pendingImport.parsed) return;
      const listSel = document.getElementById('import-target-list');
      const targetListId = parseInt(listSel ? listSel.value : '', 10) || appData.word_lists[0].id;
      const res = importWordEntries(pendingImport.parsed.entries, targetListId);
      await saveData();
      updateAllListDropdowns();
      renderAllViews();
      triggerAutoCloudPush();
      pendingImport = null;
      closeModals();
      showToast(`✅ 成功导入 ${res.imported} 个单词${res.skipped ? `，跳过重复 ${res.skipped} 个` : ''}！`, 4000);
    }

    function openBatchImportModal() {
      pendingImport = null;
      const box = document.getElementById('import-text-area');
      if (box) box.value = '';
      const fileInput = document.getElementById('import-batch-file-input');
      if (fileInput) fileInput.value = '';
      clearImportError();
      renderImportPreview();
      document.getElementById('modal-batch-import').classList.add('active');
    }

    // ── 拖拽 / 选择文件入口 ──
    function setupImportDropZone() {
      const zone = document.getElementById('import-drop-zone');
      // ⚠️ 必须用独立 ID：页面别处已有 id="import-file-input"（数据同步页的 .json 选择框），
      // 复用同一个 ID 会导致点击这里实际打开那个只认 .json 的选择框，Excel/Word 直接没法选。
      const fileInput = document.getElementById('import-batch-file-input');
      if (!zone || !fileInput) return;

      zone.addEventListener('click', () => fileInput.click());
      fileInput.addEventListener('change', (e) => {
        const f = e.target.files && e.target.files[0];
        e.target.value = '';
        if (f) handleImportFile(f);
      });

      ['dragenter', 'dragover'].forEach(ev => zone.addEventListener(ev, (e) => {
        e.preventDefault(); e.stopPropagation();
        zone.style.borderColor = '#38BDF8';
        zone.style.background = 'rgba(56,189,248,0.08)';
      }));
      ['dragleave', 'drop'].forEach(ev => zone.addEventListener(ev, (e) => {
        e.preventDefault(); e.stopPropagation();
        zone.style.borderColor = 'rgba(56,189,248,0.45)';
        zone.style.background = 'transparent';
      }));
      zone.addEventListener('drop', (e) => {
        const f = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0];
        if (f) handleImportFile(f);
      });

      // 整页拖拽：拖到任意位置都会唤起导入弹窗
      document.addEventListener('dragover', (e) => e.preventDefault());
      document.addEventListener('drop', (e) => {
        if (document.getElementById('view-photo')?.classList.contains('active') && Array.from(e.dataTransfer?.files || []).some(f => f.type.startsWith('image/'))) {
          e.preventDefault(); PhotoUI.addFiles(e.dataTransfer.files); return;
        }
        const inside = e.target && e.target.closest && e.target.closest('#modal-batch-import');
        if (inside) return;
        const f = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0];
        if (!f) return;
        e.preventDefault();
        openBatchImportModal();
        handleImportFile(f);
      });
    }
'''

PHOTO_HTML = (BASE_DIR / 'web/photo.html').read_text(encoding='utf-8')
PHOTO_CSS = (BASE_DIR / 'web/photo.css').read_text(encoding='utf-8')
PHOTO_CORE_JS = (BASE_DIR / 'web/photo_core.js').read_text(encoding='utf-8')
PHOTO_UI_JS = (BASE_DIR / 'web/photo_ui.js').read_text(encoding='utf-8')

html_content = f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no, viewport-fit=cover">
  <title>托福词汇背诵助手 (网页版)</title>
  <meta name="apple-mobile-web-app-capable" content="yes">
  <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
  <meta name="theme-color" content="#0F172A">
  <style>
    :root {{
      --bg-main: #0B0F19;
      --bg-card: #151D2F;
      --bg-card-hover: #1E293B;
      --bg-sidebar: #0E1524;
      --border-color: #243048;
      --text-main: #F8FAFC;
      --text-muted: #94A3B8;
      --text-sub: #64748B;
      --primary: #2563EB;
      --primary-hover: #1D4ED8;
      --accent-blue: #38BDF8;
      --success: #10B981;
      --warning: #F59E0B;
      --danger: #EF4444;
      --purple: #8B5CF6;
      --font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
    }}

    * {{
      box-sizing: border-box;
      margin: 0;
      padding: 0;
      -webkit-tap-highlight-color: transparent;
      touch-action: manipulation;
    }}

    body {{
      font-family: var(--font-family);
      background-color: var(--bg-main);
      color: var(--text-main);
      display: flex;
      height: 100vh;
      overflow: hidden;
    }}

    /* ── 侧边栏 (桌面端) ── */
    #sidebar {{
      width: 220px;
      background-color: var(--bg-sidebar);
      border-right: 1px solid var(--border-color);
      display: flex;
      flex-direction: column;
      flex-shrink: 0;
      z-index: 50;
      transition: all 0.2s ease;
    }}

    .sidebar-header {{
      padding: 24px 20px 16px;
      text-align: center;
      border-bottom: 1px solid var(--border-color);
    }}

    .sidebar-header h1 {{
      font-size: 1.15rem;
      font-weight: 800;
      letter-spacing: 0.5px;
      background: linear-gradient(135deg, #60A5FA, #38BDF8);
      -webkit-background-clip: text;
      -webkit-text-fill-color: transparent;
      line-height: 1.3;
    }}

    .sidebar-header p {{
      font-size: 0.75rem;
      color: var(--text-muted);
      margin-top: 4px;
    }}

    .nav-list {{
      list-style: none;
      padding: 16px 10px;
      flex: 1;
      overflow-y: auto;
      display: flex;
      flex-direction: column;
      gap: 6px;
    }}

    .nav-item {{
      display: flex;
      align-items: center;
      gap: 12px;
      padding: 10px 16px;
      border-radius: 8px;
      color: var(--text-muted);
      font-size: 0.92rem;
      font-weight: 500;
      cursor: pointer;
      transition: all 0.15s ease;
      user-select: none;
    }}

    .nav-item:hover {{
      background-color: var(--bg-card-hover);
      color: var(--text-main);
    }}

    .nav-item.active {{
      background-color: var(--primary);
      color: #FFFFFF;
      font-weight: 600;
      box-shadow: 0 4px 12px rgba(37, 99, 235, 0.35);
    }}

    .nav-item .icon {{
      font-size: 1.15rem;
    }}

    .sidebar-footer {{
      padding: 14px 16px;
      border-top: 1px solid var(--border-color);
      font-size: 0.75rem;
      color: var(--text-sub);
      text-align: center;
      line-height: 1.5;
    }}

    /* ── 主内容区 ── */
    #main-content {{
      flex: 1;
      height: 100vh;
      overflow-y: auto;
      padding: 24px 32px;
      position: relative;
    }}

    .view-page {{
      display: none;
      max-width: 1000px;
      margin: 0 auto;
      animation: fadeIn 0.18s ease-out;
    }}

    .view-page.active {{
      display: block;
    }}

    @keyframes fadeIn {{
      from {{ opacity: 0; transform: translateY(4px); }}
      to {{ opacity: 1; transform: translateY(0); }}
    }}

    /* ── 通用组件 ── */
    .page-header {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      margin-bottom: 20px;
      gap: 16px;
      flex-wrap: wrap;
    }}

    .page-title {{
      font-size: 1.45rem;
      font-weight: 700;
      display: flex;
      align-items: center;
      gap: 8px;
    }}

    .card {{
      background-color: var(--bg-card);
      border: 1px solid var(--border-color);
      border-radius: 12px;
      padding: 20px;
      box-shadow: 0 4px 16px rgba(0,0,0,0.25);
    }}

    .btn {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 6px;
      padding: 8px 16px;
      border-radius: 8px;
      font-size: 0.88rem;
      font-weight: 600;
      cursor: pointer;
      border: none;
      outline: none;
      transition: all 0.15s ease;
      user-select: none;
    }}

    .btn-primary {{ background-color: var(--primary); color: white; }}
    .btn-primary:hover {{ background-color: var(--primary-hover); }}
    .btn-secondary {{ background-color: #334155; color: white; }}
    .btn-secondary:hover {{ background-color: #475569; }}
    .btn-success {{ background-color: var(--success); color: white; }}
    .btn-danger {{ background-color: var(--danger); color: white; }}
    .btn-outline {{ background-color: transparent; border: 1px solid var(--border-color); color: var(--text-main); }}
    .btn-outline:hover {{ background-color: var(--bg-card-hover); }}

    .badge {{
      display: inline-flex;
      align-items: center;
      padding: 3px 8px;
      border-radius: 6px;
      font-size: 0.75rem;
      font-weight: 700;
      color: white;
    }}

    .input-field {{
      width: 100%;
      background-color: #0F172A;
      border: 1px solid var(--border-color);
      border-radius: 8px;
      padding: 10px 14px;
      color: var(--text-main);
      font-size: 0.95rem;
      outline: none;
      transition: border-color 0.15s;
    }}

    .input-field:focus {{
      border-color: var(--primary);
    }}

    select.input-field {{
      cursor: pointer;
      appearance: none;
      background-image: url("data:image/svg+xml;charset=UTF-8,%3csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='%2394A3B8' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'%3e%3cpolyline points='6 9 12 15 18 9'%3e%3c/polyline%3e%3c/svg%3e");
      background-repeat: no-repeat;
      background-position: right 12px center;
      background-size: 16px;
      padding-right: 36px;
    }}

    /* ── 底部移动端导航 ── */
    #mobile-bottom-nav {{
      display: none;
      position: fixed;
      bottom: 0;
      left: 0;
      right: 0;
      background-color: var(--bg-sidebar);
      border-top: 1px solid var(--border-color);
      padding: 8px 6px calc(8px + env(safe-area-inset-bottom));
      z-index: 100;
      justify-content: space-around;
    }}

    .mobile-nav-item {{
      display: flex;
      flex-direction: column;
      align-items: center;
      gap: 3px;
      color: var(--text-muted);
      font-size: 0.68rem;
      cursor: pointer;
      padding: 4px 8px;
      border-radius: 6px;
    }}

    .mobile-nav-item.active {{
      color: #60A5FA;
      font-weight: 700;
    }}

    .mobile-nav-item .icon {{
      font-size: 1.25rem;
    }}

    /* ── 弹窗 Modal ── */
    .modal-overlay {{
      display: none;
      position: fixed;
      inset: 0;
      background-color: rgba(0,0,0,0.65);
      backdrop-filter: blur(4px);
      z-index: 200;
      align-items: center;
      justify-content: center;
      padding: 16px;
    }}

    .modal-overlay.active {{
      display: flex;
    }}

    .modal-card {{
      background-color: var(--bg-card);
      border: 1px solid var(--border-color);
      border-radius: 14px;
      width: 100%;
      max-width: 500px;
      max-height: 90vh;
      overflow-y: auto;
      padding: 24px;
      box-shadow: 0 10px 30px rgba(0,0,0,0.5);
      animation: modalPop 0.18s ease-out;
    }}

    @keyframes modalPop {{
      from {{ opacity: 0; transform: scale(0.96); }}
      to {{ opacity: 1; transform: scale(1); }}
    }}

    /* ── Toast 提示 ── */
    #toast {{
      position: fixed;
      top: 24px;
      right: 24px;
      background-color: #1E293B;
      border: 1px solid var(--border-color);
      color: white;
      padding: 12px 20px;
      border-radius: 8px;
      font-size: 0.9rem;
      font-weight: 600;
      box-shadow: 0 8px 24px rgba(0,0,0,0.4);
      z-index: 300;
      opacity: 0;
      pointer-events: none;
      transform: translateY(-10px);
      transition: all 0.2s ease;
    }}

    #toast.show {{
      opacity: 1;
      transform: translateY(0);
    }}

    /* 响应式断点 */
    @media (max-width: 768px) {{
      #sidebar {{ display: none; }}
      #mobile-bottom-nav {{ display: flex; }}
      #main-content {{
        padding: 16px 14px calc(76px + env(safe-area-inset-bottom));
        height: auto;
        min-height: 100vh;
      }}
      .page-header {{
        flex-direction: column;
        align-items: flex-start;
      }}
    }}
{PHOTO_CSS}
  </style>
</head>
<body>

  <!-- ── 桌面端侧边栏 ── -->
  <aside id="sidebar">
    <div class="sidebar-header">
      <h1>托福词汇<br>背诵助手</h1>
      <p>智能排期 · 专项词单 · 全端互通</p>
    </div>
    <ul class="nav-list">
      <li class="nav-item active" data-tab="add"><span class="icon">📥</span> 录入单词</li>
      <li class="nav-item" data-tab="photo"><span class="icon">📷</span> 拍照测词</li>
      <li class="nav-item" data-tab="review"><span class="icon">📅</span> 今日复习</li>
      <li class="nav-item" data-tab="sweep"><span class="icon">📖</span> 拉通复习</li>
      <li class="nav-item" data-tab="quiz"><span class="icon">🧠</span> 随时自测</li>
      <li class="nav-item" data-tab="words"><span class="icon">📚</span> 我的词库</li>
      <li class="nav-item" data-tab="stats"><span class="icon">📊</span> 学习统计</li>
      <li class="nav-item" data-tab="sync"><span class="icon">🔄</span> 数据同步</li>
    </ul>
    <div class="sidebar-footer" id="sidebar-status">
      总词数：-- 个<br>今日待复习：-- 个
    </div>
    <div id="app-build-stamp" style="font-size: 0.66rem; color: #475569; text-align: center; padding: 4px 8px 10px; line-height: 1.5;"></div>
  </aside>

  <!-- ── 主内容区 ── -->
  <main id="main-content">
{PHOTO_HTML}

    <!-- 1. 📥 录入单词 -->
    <section id="view-add" class="view-page active">
      <div class="page-header">
        <h2 class="page-title">📥 录入词汇</h2>
        <div style="display: flex; gap: 8px; align-items: center;">
          <span style="font-size: 0.85rem; color: var(--text-muted);">目标词单：</span>
          <select id="add-list-select" class="input-field" style="width: 170px; padding: 6px 10px;"></select>
          <button class="btn btn-primary" onclick="openNewListModal()">➕ 新建词单</button>
          <button class="btn btn-secondary" onclick="openBatchImportModal()">📋 批量导入</button>
        </div>
      </div>

      <div class="card" style="margin-bottom: 16px; background-color: #1E293B; border-color: #334155; padding: 12px 16px; display: flex; justify-content: space-between; align-items: center;">
        <span style="color: #93C5FD; font-size: 0.85rem;">💡 技巧：输入英文后直接按【Enter 回车】即可自动拉取释义并朗读！再次回车直接存入词单。</span>
        <span id="storage-indicator" class="badge" style="background-color: #059669; font-size: 0.72rem; white-space: nowrap;">🟢 双重持久化存储保护中</span>
      </div>

      <div class="card">
        <div style="margin-bottom: 16px;">
          <label style="display: block; font-size: 0.85rem; margin-bottom: 6px; font-weight: 600;">英文单词 *</label>
          <div style="display: flex; gap: 8px;">
            <input type="text" id="add-word-input" class="input-field" placeholder="输入英文单词，如：ubiquitous" autofocus autocomplete="off">
            <button class="btn btn-primary" id="btn-lookup" onclick="lookupWordAction()" style="white-space: nowrap; width: 110px;">🔍 查释义</button>
          </div>
        </div>

        <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 14px; margin-bottom: 14px;">
          <div>
            <label style="display: block; font-size: 0.85rem; margin-bottom: 6px;">音标</label>
            <input type="text" id="add-phonetic-input" class="input-field" placeholder="/.../">
          </div>
          <div>
            <label style="display: block; font-size: 0.85rem; margin-bottom: 6px;">中文释义 *</label>
            <input type="text" id="add-meaning-input" class="input-field" placeholder="输入或回车自动查中文">
          </div>
        </div>

        <div style="margin-bottom: 14px;">
          <label style="display: block; font-size: 0.85rem; margin-bottom: 6px;">真题原句 / 双语例句</label>
          <input type="text" id="add-example-input" class="input-field" placeholder="例句语境">
        </div>

        <div style="margin-bottom: 20px;">
          <label style="display: block; font-size: 0.85rem; margin-bottom: 6px;">个人笔记 / 考点出处（可选）</label>
          <input type="text" id="add-note-input" class="input-field" placeholder="如：TPO 54 阅读错题、同义替换词">
        </div>

        <div style="display: flex; gap: 10px;">
          <button class="btn btn-primary" style="flex: 2; height: 42px; font-size: 0.95rem;" onclick="saveNewWordAction()">✅ 确认录入词库 (Enter)</button>
          <button class="btn btn-outline" style="flex: 1;" onclick="clearAddForm()">清空重填</button>
        </div>
      </div>
    </section>

    <!-- 2. 📅 今日复习 -->
    <section id="view-review" class="view-page">
      <div class="page-header">
        <h2 class="page-title">📅 今日复习 <span id="rev-progress-counter" style="font-size: 0.95rem; color: var(--text-muted); font-weight: normal; margin-left: 10px;">0 / 0</span></h2>
        <div style="display: flex; gap: 8px; align-items: center;">
          <span style="font-size: 0.85rem; color: var(--text-muted);">词单范围：</span>
          <select id="rev-list-filter" class="input-field" style="width: 170px; padding: 6px 10px;" onchange="loadReviewQueue()"></select>
        </div>
      </div>

      <!-- 进度条 -->
      <div style="width: 100%; height: 6px; background-color: #334155; border-radius: 3px; margin-bottom: 14px; overflow: hidden;">
        <div id="rev-progress-bar" style="width: 0%; height: 100%; background: linear-gradient(90deg, #2563EB, #38BDF8); transition: width 0.2s;"></div>
      </div>

      <!-- 实时测算指标看板 -->
      <div id="rev-metrics-strip" style="display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px; margin-bottom: 16px; background-color: var(--bg-card); border: 1px solid var(--border-color); border-radius: 10px; padding: 10px 14px; text-align: center;">
        <div style="font-size: 0.82rem; color: var(--text-muted);">
          <span>⏳ 待掌握</span>
          <div id="rev-metric-pending" style="font-size: 1.15rem; font-weight: 700; color: #F8FAFC; margin-top: 2px;">0</div>
        </div>
        <div style="font-size: 0.82rem; color: var(--text-muted);">
          <span>🔄 巩固中</span>
          <div id="rev-metric-relearning" style="font-size: 1.15rem; font-weight: 700; color: #F97316; margin-top: 2px;">0</div>
        </div>
        <div style="font-size: 0.82rem; color: var(--text-muted);">
          <span>✅ 已掌握</span>
          <div id="rev-metric-mastered" style="font-size: 1.15rem; font-weight: 700; color: #10B981; margin-top: 2px;">0</div>
        </div>
        <div style="font-size: 0.82rem; color: var(--text-muted);">
          <span>📊 实时保留率</span>
          <div id="rev-metric-retention" style="font-size: 1.15rem; font-weight: 700; color: #38BDF8; margin-top: 2px;">100%</div>
        </div>
      </div>

      <!-- 复习卡片 -->
      <div id="rev-card-container" class="card" style="min-height: 380px; display: flex; flex-direction: column; justify-content: space-between;">
        <div>
          <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px;">
            <div style="display: flex; gap: 8px; align-items: center; flex-wrap: wrap;">
              <span id="rev-card-list-badge" class="badge">--</span>
              <span id="rev-card-state-text" style="font-size: 0.85rem; color: var(--text-muted);">--</span>
              <span id="rev-card-relearn-badge" class="badge" style="display: none; background-color: #EA580C; color: white;">⚡️ 动态巩固第 1 轮 · 上次未记牢</span>
            </div>
            <button class="btn btn-outline" style="padding: 4px 10px; font-size: 0.8rem;" onclick="speakCurrentWord()">🔊 读音 (R)</button>
          </div>

          <div style="text-align: center; margin: 24px 0 16px;">
            <h1 id="rev-card-word" style="font-size: 2.8rem; font-weight: 800; letter-spacing: 0.5px;">Word</h1>
            <p id="rev-card-phonetic" style="font-size: 1.15rem; color: var(--accent-blue); margin-top: 6px; font-family: monospace;">/.../</p>
          </div>

          <!-- 未翻转遮罩 -->
          <div id="rev-cover" style="background-color: #0B1120; border: 1px dashed var(--border-color); border-radius: 12px; padding: 40px 16px; text-align: center; cursor: pointer;" onclick="revealReviewCard()">
            <p style="font-size: 1.05rem; color: #94A3B8; font-weight: 500;">👉 点击此处 或 按【空格 Space / 回车】查看释义</p>
          </div>

          <!-- 翻转后答案 -->
          <div id="rev-answer" style="display: none; background-color: #0F172A; border-radius: 12px; padding: 18px 20px; text-align: left;">
            <p id="rev-card-meaning" style="font-size: 1.25rem; font-weight: 600; color: #F1F5F9; margin-bottom: 10px;">释义</p>
            <p id="rev-card-example" style="font-size: 0.95rem; color: #CBD5E1; margin-bottom: 8px; line-height: 1.4;"></p>
            <p id="rev-card-note" style="font-size: 0.85rem; color: #86EFAC;"></p>
          </div>
        </div>

        <!-- 底部操作区 -->
        <div style="margin-top: 24px;">
          <button id="rev-btn-reveal" class="btn btn-primary" style="width: 100%; height: 46px; font-size: 1rem;" onclick="revealReviewCard()">👁 翻开答案 (Space)</button>

          <div id="rev-rating-buttons" style="display: none; grid-template-columns: repeat(4, 1fr); gap: 10px;">
            <button class="btn btn-danger" style="height: 52px; flex-direction: column; font-size: 0.85rem;" onclick="rateReviewCard(1)">
              <span>[1] Again</span><span id="rev-btn-hint-1" style="font-size: 0.72rem; font-weight: normal;">忘记了 (稍后重学)</span>
            </button>
            <button class="btn" style="background-color: var(--warning); color: white; height: 52px; flex-direction: column; font-size: 0.85rem;" onclick="rateReviewCard(2)">
              <span>[2] Hard</span><span id="rev-btn-hint-2" style="font-size: 0.72rem; font-weight: normal;">模糊 (+1天)</span>
            </button>
            <button class="btn btn-success" style="height: 52px; flex-direction: column; font-size: 0.85rem;" onclick="rateReviewCard(3)">
              <span>[3] Good</span><span id="rev-btn-hint-3" style="font-size: 0.72rem; font-weight: normal;">记住了 (Space)</span>
            </button>
            <button class="btn" style="background-color: #2563EB; color: white; height: 52px; flex-direction: column; font-size: 0.85rem;" onclick="rateReviewCard(4)">
              <span>[4] Easy</span><span id="rev-btn-hint-4" style="font-size: 0.72rem; font-weight: normal;">很简单 (+8天)</span>
            </button>
          </div>
        </div>
      </div>

      <!-- 完成空状态与战报 -->
      <div id="rev-empty-state" class="card" style="display: none; padding: 48px 20px; text-align: center;">
        <div style="font-size: 3.5rem; margin-bottom: 12px;">🎉</div>
        <h3 id="rev-empty-title" style="font-size: 1.4rem; font-weight: 700; margin-bottom: 8px;">今日复习已完成！</h3>
        <p id="rev-empty-desc" style="color: var(--text-muted); font-size: 0.95rem; margin-bottom: 20px;">生词与遗忘词汇已在动态闭环中全部彻底掌握！</p>
        
        <div id="rev-summary-stats" style="display: none; grid-template-columns: repeat(4, 1fr); gap: 12px; max-width: 600px; margin: 0 auto 24px; background: rgba(30, 41, 59, 0.5); border: 1px solid var(--border-color); border-radius: 12px; padding: 16px;">
          <div>
            <div style="font-size: 0.8rem; color: var(--text-muted);">攻克掌握</div>
            <div id="rev-summary-mastered" style="font-size: 1.4rem; font-weight: 800; color: #10B981; margin-top: 4px;">0</div>
          </div>
          <div>
            <div style="font-size: 0.8rem; color: var(--text-muted);">初次保留率</div>
            <div id="rev-summary-retention" style="font-size: 1.4rem; font-weight: 800; color: #38BDF8; margin-top: 4px;">100%</div>
          </div>
          <div>
            <div style="font-size: 0.8rem; color: var(--text-muted);">消灭生词</div>
            <div id="rev-summary-relearned" style="font-size: 1.4rem; font-weight: 800; color: #F97316; margin-top: 4px;">0</div>
          </div>
          <div>
            <div style="font-size: 0.8rem; color: var(--text-muted);">互动轮次</div>
            <div id="rev-summary-rounds" style="font-size: 1.4rem; font-weight: 800; color: #A855F7; margin-top: 4px;">0</div>
          </div>
        </div>

        <div style="display: flex; gap: 12px; justify-content: center; flex-wrap: wrap;">
          <button class="btn btn-outline" onclick="loadReviewQueue()">🔄 重新过一遍</button>
          <button class="btn btn-primary" onclick="switchTab('add')">➕ 去录入新词</button>
          <button class="btn btn-success" onclick="switchTab('quiz')">🧠 去随时自测</button>
        </div>
      </div>
    </section>

    <!-- 2.5. 📖 拉通复习 -->
    <section id="view-sweep" class="view-page">
      <div class="page-header">
        <h2 class="page-title">📖 拉通复习 <span id="sweep-progress-counter" style="font-size: 0.95rem; color: var(--text-muted); font-weight: normal; margin-left: 10px;">0 / 0</span></h2>
        <div style="display: flex; gap: 8px; align-items: center; flex-wrap: wrap;">
          <span style="font-size: 0.85rem; color: var(--text-muted);">词单：</span>
          <select id="sweep-list-filter" class="input-field" style="width: 160px; padding: 6px 10px;"></select>
          <span style="font-size: 0.85rem; color: var(--text-muted);">每批：</span>
          <select id="sweep-batch-size" class="input-field" style="width: 100px; padding: 6px 10px;">
            <option value="0">全部</option>
            <option value="10">10 词</option>
            <option value="20" selected>20 词</option>
            <option value="30">30 词</option>
            <option value="50">50 词</option>
          </select>
          <select id="sweep-order" class="input-field" style="width: 110px; padding: 6px 10px;">
            <option value="random">随机顺序</option>
            <option value="seq">按序过一遍</option>
          </select>
          <button class="btn btn-primary" onclick="initSweepReview()">🚀 开始拉通</button>
        </div>
      </div>

      <!-- 说明提示 -->
      <div class="card" style="margin-bottom: 14px; background-color: #1E293B; border-color: #334155; padding: 10px 16px; font-size: 0.85rem; color: #93C5FD; line-height: 1.6;">
        💡 <b>拉通复习</b>：一次性过一遍选定词单中的词，<b>不受 FSRS 到期限制</b>。答错立即进入今日复习强化！<br>
        评分规则：[1] 忘了 → 加入今日复习 &nbsp;&nbsp;[2] 模糊 → 加入今日复习 &nbsp;&nbsp;[3] 记住了 → 不额外安排 &nbsp;&nbsp;[4] 太简单 → 跳过
      </div>

      <!-- 进度条 -->
      <div style="width: 100%; height: 6px; background-color: #334155; border-radius: 3px; margin-bottom: 14px; overflow: hidden;">
        <div id="sweep-progress-bar" style="width: 0%; height: 100%; background: linear-gradient(90deg, #7C3AED, #A78BFA); transition: width 0.2s;"></div>
      </div>

      <!-- 起始空状态（未开始） -->
      <div id="sweep-start-state" class="card" style="padding: 48px 20px; text-align: center;">
        <div style="font-size: 3.5rem; margin-bottom: 12px;">📖</div>
        <h3 style="font-size: 1.4rem; font-weight: 700; margin-bottom: 8px;">选择词单和批次，开始拉通复习</h3>
        <p style="color: var(--text-muted); font-size: 0.95rem; margin-bottom: 20px;">可以一次性过完整个词单，也可以每次只过 10/20/30 个词。<br>答错的词自动加入今日复习，结束后可立即跳转强化！</p>
        <button class="btn btn-primary" style="font-size: 1rem; padding: 12px 28px;" onclick="initSweepReview()">🚀 立即开始拉通复习</button>
      </div>

      <!-- 复习卡片 -->
      <div id="sweep-card-container" class="card" style="display: none; min-height: 380px; display: none; flex-direction: column; justify-content: space-between;">
        <div>
          <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px;">
            <div style="display: flex; gap: 8px; align-items: center; flex-wrap: wrap;">
              <span id="sweep-card-list-badge" class="badge">--</span>
              <span id="sweep-card-state-text" style="font-size: 0.85rem; color: var(--text-muted);">--</span>
            </div>
            <button class="btn btn-outline" style="padding: 4px 10px; font-size: 0.8rem;" onclick="sweepSpeakCurrent()">🔊 读音 (R)</button>
          </div>

          <div style="text-align: center; margin: 24px 0 16px;">
            <h1 id="sweep-card-word" style="font-size: 2.8rem; font-weight: 800; letter-spacing: 0.5px;">Word</h1>
            <p id="sweep-card-phonetic" style="font-size: 1.15rem; color: var(--accent-blue); margin-top: 6px; font-family: monospace;">/.../</p>
          </div>

          <!-- 未翻转遮罩 -->
          <div id="sweep-cover" style="background-color: #0B1120; border: 1px dashed var(--border-color); border-radius: 12px; padding: 40px 16px; text-align: center; cursor: pointer;" onclick="revealSweepCard()">
            <p style="font-size: 1.05rem; color: #94A3B8; font-weight: 500;">👉 点击此处 或 按【空格 Space / 回车】查看释义</p>
          </div>

          <!-- 翻转后答案 -->
          <div id="sweep-answer" style="display: none; background-color: #0F172A; border-radius: 12px; padding: 18px 20px; text-align: left;">
            <p id="sweep-card-meaning" style="font-size: 1.25rem; font-weight: 600; color: #F1F5F9; margin-bottom: 10px;">释义</p>
            <p id="sweep-card-example" style="font-size: 0.95rem; color: #CBD5E1; margin-bottom: 8px; line-height: 1.4;"></p>
            <p id="sweep-card-note" style="font-size: 0.85rem; color: #86EFAC;"></p>
          </div>
        </div>

        <!-- 底部操作区 -->
        <div style="margin-top: 24px;">
          <button id="sweep-btn-reveal" class="btn btn-primary" style="width: 100%; height: 46px; font-size: 1rem;" onclick="revealSweepCard()">👁 翻开答案 (Space)</button>
          <div id="sweep-rating-buttons" style="display: none; grid-template-columns: repeat(4, 1fr); gap: 10px;">
            <button class="btn btn-danger" style="height: 52px; flex-direction: column; font-size: 0.85rem;" onclick="rateSweepCard(1)">
              <span>[1] 忘了</span><span style="font-size: 0.72rem; font-weight: normal;">→ 入今日复习</span>
            </button>
            <button class="btn" style="background-color: var(--warning); color: white; height: 52px; flex-direction: column; font-size: 0.85rem;" onclick="rateSweepCard(2)">
              <span>[2] 模糊</span><span style="font-size: 0.72rem; font-weight: normal;">→ 入今日复习</span>
            </button>
            <button class="btn btn-success" style="height: 52px; flex-direction: column; font-size: 0.85rem;" onclick="rateSweepCard(3)">
              <span>[3] 记住了</span><span style="font-size: 0.72rem; font-weight: normal;">继续 (Space)</span>
            </button>
            <button class="btn" style="background-color: #2563EB; color: white; height: 52px; flex-direction: column; font-size: 0.85rem;" onclick="rateSweepCard(4)">
              <span>[4] 太简单</span><span style="font-size: 0.72rem; font-weight: normal;">直接跳过</span>
            </button>
          </div>
        </div>
      </div>

      <!-- 完成空状态与战报 -->
      <div id="sweep-done-state" class="card" style="display: none; padding: 48px 20px; text-align: center;">
        <div style="font-size: 3.5rem; margin-bottom: 12px;">🏁</div>
        <h3 id="sweep-done-title" style="font-size: 1.4rem; font-weight: 700; margin-bottom: 8px;">本批拉通完成！</h3>
        <p id="sweep-done-desc" style="color: var(--text-muted); font-size: 0.95rem; margin-bottom: 20px;"></p>

        <!-- 战报统计 -->
        <div id="sweep-summary-stats" style="display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; max-width: 500px; margin: 0 auto 24px; background: rgba(30, 41, 59, 0.5); border: 1px solid var(--border-color); border-radius: 12px; padding: 16px;">
          <div>
            <div style="font-size: 0.8rem; color: var(--text-muted);">总计复习</div>
            <div id="sweep-stat-total" style="font-size: 1.4rem; font-weight: 800; color: #38BDF8; margin-top: 4px;">0</div>
          </div>
          <div>
            <div style="font-size: 0.8rem; color: var(--text-muted);">记住了</div>
            <div id="sweep-stat-known" style="font-size: 1.4rem; font-weight: 800; color: #10B981; margin-top: 4px;">0</div>
          </div>
          <div>
            <div style="font-size: 0.8rem; color: var(--text-muted);">加入复习</div>
            <div id="sweep-stat-queued" style="font-size: 1.4rem; font-weight: 800; color: #F97316; margin-top: 4px;">0</div>
          </div>
        </div>

        <!-- 有答错词时提示跳转 -->
        <div id="sweep-goto-review-tip" style="display: none; margin-bottom: 20px; padding: 12px 16px; background: rgba(234, 88, 12, 0.15); border: 1px solid #EA580C; border-radius: 10px; font-size: 0.95rem; color: #FED7AA;">
          ⚠️ 有 <span id="sweep-queued-count" style="font-weight: 800; color: #FB923C;">0</span> 个词答错已加入今日复习！建议立即前往强化背诵，确保 100% 掌握。
        </div>

        <div style="display: flex; gap: 12px; justify-content: center; flex-wrap: wrap;">
          <button class="btn btn-outline" onclick="initSweepReview()">🔄 再来一批</button>
          <button id="sweep-btn-goto-review" class="btn btn-danger" style="display: none;" onclick="switchTab('review')">🔥 立即去今日复习强化！</button>
          <button class="btn btn-primary" onclick="switchTab('review')">📅 去今日复习</button>
        </div>
      </div>
    </section>

    <!-- 3. 🧠 随时自测 -->
    <section id="view-quiz" class="view-page">
      <div class="page-header">
        <h2 class="page-title">🧠 随时自测</h2>
        <div style="display: flex; gap: 8px; align-items: center; flex-wrap: wrap;">
          <span style="font-size: 0.85rem; color: var(--text-muted);">范围：</span>
          <select id="quiz-list-filter" class="input-field" style="width: 150px; padding: 6px 10px;" onchange="nextQuizQuestion()"></select>
          <select id="quiz-type-select" class="input-field" style="width: 110px; padding: 6px 10px;" onchange="nextQuizQuestion()">
            <option value="random">随机题型</option>
            <option value="flash">👀 认不认识</option>
            <option value="choice">选择题</option>
            <option value="spell">拼写题</option>
            <option value="blank">填空题</option>
          </select>
          <button class="btn btn-secondary" onclick="nextQuizQuestion()">⏭ 下一题</button>
        </div>
      </div>

      <div class="card" style="min-height: 380px;">
        <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px;">
          <span id="quiz-list-badge" class="badge">--</span>
          <span id="quiz-type-badge" style="font-size: 0.85rem; color: var(--text-muted);">--</span>
        </div>

        <h3 id="quiz-question-title" style="font-size: 1.25rem; font-weight: 600; margin-bottom: 20px; line-height: 1.4;">--</h3>

        <!-- 认不认识卡片选项区域 -->
        <div id="quiz-flash-container" style="display: none; gap: 12px; margin-bottom: 20px;">
          <button id="btn-flash-know" class="btn" style="flex: 1; padding: 14px 20px; font-size: 1.05rem; font-weight: 700; background-color: #10B981; color: white;" onclick="onFlashSelect(true)">👀 认识 (记得) [1]</button>
          <button id="btn-flash-dont-know" class="btn btn-danger" style="flex: 1; padding: 14px 20px; font-size: 1.05rem; font-weight: 700;" onclick="onFlashSelect(false)">🤷‍♂️ 不认识 (忘记了) [2]</button>
        </div>

        <!-- 选择题区域 -->
        <div id="quiz-choices-container" style="display: flex; flex-direction: column; gap: 10px; margin-bottom: 20px;"></div>

        <!-- 拼写/填空输入区域 -->
        <div id="quiz-input-container" style="display: none; margin-bottom: 20px;">
          <div style="display: flex; gap: 10px;">
            <input type="text" id="quiz-answer-input" class="input-field" placeholder="在此输入英文单词并按回车提交" autocomplete="off">
            <button class="btn btn-primary" style="white-space: nowrap;" onclick="submitQuizInput()">提交 (Enter)</button>
          </div>
        </div>

        <!-- 反馈与自评 -->
        <div id="quiz-feedback-box" style="display: none; padding: 14px 18px; border-radius: 8px; margin-top: 14px; background-color: #0F172A; border: 1px solid var(--border-color);">
          <p id="quiz-feedback-text" style="font-weight: 600; margin-bottom: 8px; font-size: 1.05rem;"></p>
          <div style="display: flex; gap: 8px; margin-top: 12px; align-items: center; flex-wrap: wrap;">
            <button id="btn-quiz-force-review" class="btn btn-outline" style="padding: 5px 12px; font-size: 0.82rem; border-color: #38BDF8; color: #38BDF8; font-weight: 600;" onclick="addQuizWordToReviewDirectly()">📥 放入今日复习</button>
            <button id="btn-quiz-actually-dont-know" class="btn" style="display: none; padding: 5px 12px; font-size: 0.82rem; background-color: #EA580C; color: white; font-weight: 600;" onclick="onActuallyDontKnow()">🤔 其实不认识 (记错了) [0]</button>
            <button id="btn-quiz-confirm-know" class="btn" style="display: none; padding: 5px 12px; font-size: 0.82rem; background-color: #10B981; color: white; font-weight: 600;" onclick="rateQuizCard(3)">✅ 确实记对了 (Space)</button>
            <span style="font-size: 0.8rem; color: var(--text-muted); margin-left: 6px;">评分：</span>
            <button class="btn btn-danger" style="padding: 4px 10px; font-size: 0.78rem;" onclick="rateQuizCard(1)">[1] 忘了 (入复习)</button>
            <button class="btn" style="background-color: var(--warning); color: white; padding: 4px 10px; font-size: 0.78rem;" onclick="rateQuizCard(2)">[2] 模糊</button>
            <button class="btn btn-success" style="padding: 4px 10px; font-size: 0.78rem;" onclick="rateQuizCard(3)">[3] 记住了</button>
            <button class="btn btn-primary" style="padding: 4px 10px; font-size: 0.78rem;" onclick="rateQuizCard(4)">[4] 简单</button>
          </div>
        </div>
      </div>

      <div style="margin-top: 12px; color: var(--text-sub); font-size: 0.8rem;">
        ⌨️ 键盘快捷键：认不认识按 [1/2] · 选认识后若记错按 [0] 更正 · 选择题按 [A~D 或 1~4] · [空格 Space] 推进 · [R] 读音
      </div>
    </section>

    <!-- 4. 📚 我的词库 -->
    <section id="view-words" class="view-page">
      <div class="page-header">
        <h2 class="page-title">📚 我的词库</h2>
        <div style="display: flex; gap: 8px; align-items: center; flex-wrap: wrap;">
          <input type="text" id="words-search-input" class="input-field" placeholder="🔍 搜索单词、释义或备注…" style="width: 220px; padding: 6px 12px;" oninput="renderWordListTable()">
          <select id="words-list-filter" class="input-field" style="width: 170px; padding: 6px 10px;" onchange="renderWordListTable()"></select>
          <button class="btn btn-primary" onclick="openNewListModal()">➕ 新建</button>
          <button class="btn btn-secondary" id="btn-rename-list" onclick="openRenameListModal()">✏️ 重命名</button>
          <button class="btn btn-danger" id="btn-delete-list" onclick="deleteCurrentListAction()">🗑 删除</button>
        </div>
      </div>

      <div style="display: flex; justify-content: space-between; color: var(--text-muted); font-size: 0.85rem; margin-bottom: 10px;">
        <span id="words-count-info">共 -- 个单词</span>
      </div>

      <div id="word-cards-list" style="display: flex; flex-direction: column; gap: 8px;"></div>
    </section>

    <!-- 5. 📊 学习统计 -->
    <section id="view-stats" class="view-page">
      <div class="page-header">
        <h2 class="page-title">📊 学习与词库统计</h2>
        <button class="btn btn-outline" onclick="renderStatsView()">🔄 刷新统计</button>
      </div>

      <!-- 4 个统计大卡片 -->
      <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; margin-bottom: 20px;">
        <div class="card" style="background: linear-gradient(135deg, #1E3A8A, #1D4ED8);">
          <p style="font-size: 0.8rem; color: #BFDBFE;">📚 词库总词量</p>
          <h2 id="stat-total-words" style="font-size: 2.2rem; font-weight: 800; margin: 4px 0;">0</h2>
          <p id="stat-total-lists-sub" style="font-size: 0.75rem; color: #DBEAFE;">-- 个独立词单</p>
        </div>
        <div class="card" style="background: linear-gradient(135deg, #075985, #0284C7);">
          <p style="font-size: 0.8rem; color: #BAE6FD;">⏰ 今日待复习</p>
          <h2 id="stat-due-today" style="font-size: 2.2rem; font-weight: 800; margin: 4px 0;">0</h2>
          <p style="font-size: 0.75rem; color: #E0F2FE;">FSRS 记忆曲线调度</p>
        </div>
        <div class="card" style="background: linear-gradient(135deg, #065F46, #059669);">
          <p style="font-size: 0.8rem; color: #A7F3D0;">✅ 长期掌握词</p>
          <h2 id="stat-mastered-words" style="font-size: 2.2rem; font-weight: 800; margin: 4px 0;">0</h2>
          <p style="font-size: 0.75rem; color: #D1FAE5;">稳定性 > 21 天</p>
        </div>
        <div class="card" style="background: linear-gradient(135deg, #5B21B6, #7C3AED);">
          <p style="font-size: 0.8rem; color: #DDD6FE;">🆕 待学新词</p>
          <h2 id="stat-new-words" style="font-size: 2.2rem; font-weight: 800; margin: 4px 0;">0</h2>
          <p style="font-size: 0.75rem; color: #EDE9FE;">未安排初次学习</p>
        </div>
      </div>

      <!-- 各词单背诵进度卡片列表 -->
      <div class="card" style="margin-bottom: 20px;">
        <h3 style="font-size: 1.05rem; font-weight: 700; color: #93C5FD; margin-bottom: 14px;">📑 各词单背诵进度一览</h3>
        <div id="stats-lists-breakdown" style="display: flex; flex-direction: column; gap: 10px;"></div>
      </div>

      <!-- 图表区域 -->
      <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 16px;">
        <div class="card">
          <h4 style="font-size: 0.95rem; font-weight: 600; margin-bottom: 12px; text-align: center;">各词单词汇分布</h4>
          <canvas id="chart-lists-pie" width="300" height="220" style="width: 100%; height: 220px;"></canvas>
        </div>
        <div class="card">
          <h4 style="font-size: 0.95rem; font-weight: 600; margin-bottom: 12px; text-align: center;">整体掌握度分布</h4>
          <canvas id="chart-state-pie" width="300" height="220" style="width: 100%; height: 220px;"></canvas>
        </div>
      </div>
    </section>

      <!-- 6. 🔄 数据同步 -->
    <section id="view-sync" class="view-page">
      <div class="page-header">
        <h2 class="page-title">🔄 数据同步与多端互通</h2>
      </div>

      <!-- ☁️ 云端实时同步与多端网络互通 -->
      <div class="card" style="margin-bottom: 18px; border: 1px solid #2563EB;">
        <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px;">
          <h3 style="font-size: 1.05rem; font-weight: 700; color: #60A5FA;">☁️ 云端实时同步与多端网络互通</h3>
          <span id="web-cloud-status-badge" style="font-size: 0.75rem; padding: 2px 8px; border-radius: 9999px; background: rgba(148,163,184,0.2); color: #94A3B8;">⚪️ 离线/未连接</span>
        </div>
        <p style="font-size: 0.85rem; color: var(--text-muted); margin-bottom: 12px;">
          直接与 Python 客户端（或公网云服务器 / GitHub Gist）进行秒级智能双向对齐，手机与电脑学习进度无缝互通。
        </p>

        <div style="background: rgba(30, 41, 59, 0.8); border: 1px solid rgba(56, 189, 248, 0.25); border-radius: 8px; padding: 10px 14px; margin-bottom: 14px; font-size: 0.82rem; line-height: 1.6; color: #CBD5E1;">
          💡 <b>本机同步说明</b>：浏览器受安全沙箱保护，无法直接读写本地磁盘数据库文件。<br>
          • <b>全自动同步</b>：启动 <b>Python 客户端</b>（已内置启动同步服务），在浏览器访问 <a href="http://localhost:8765" target="_blank" style="color: #38BDF8; font-weight: 700; text-decoration: underline;">http://localhost:8765</a> 或点击下方【立即双向云同步】即可！<br>
          • <b>离线免网互通</b>：亦可在下方【导出同步文件 / 导入同步文件】或【复制文本 / 粘贴导入】，一键秒级互通。
        </div>

        <!-- 云端方式切换 -->
        <div style="display: flex; gap: 8px; margin-bottom: 14px;">
          <button id="tab-btn-cloud-server" class="btn btn-primary" style="font-size: 0.82rem; padding: 5px 12px;" onclick="switchCloudMode('server')">🌐 云服务器 / 局域网</button>
          <button id="tab-btn-cloud-gist" class="btn btn-secondary" style="font-size: 0.82rem; padding: 5px 12px;" onclick="switchCloudMode('gist')">🐙 GitHub Gist 专属云</button>
        </div>

        <!-- 模式1: 云端/局域网服务器 -->
        <div id="cloud-server-pane">
          <div style="display: flex; gap: 10px; margin-bottom: 10px; flex-wrap: wrap;">
            <div style="flex: 2; min-width: 220px;">
              <label style="font-size: 0.8rem; font-weight: 600; color: #94A3B8;">服务器地址 (URL)：</label>
              <input type="text" id="cloud-server-url" class="form-input" style="margin-top: 4px;" placeholder="例如 http://192.168.1.5:8765 或 http://localhost:8765">
            </div>
            <div style="flex: 1; min-width: 140px;">
              <label style="font-size: 0.8rem; font-weight: 600; color: #94A3B8;">访问令牌 (Token)：</label>
              <input type="password" id="cloud-server-token" class="form-input" style="margin-top: 4px;" placeholder="免密可留空">
            </div>
          </div>
          <div style="margin-bottom: 10px; display: flex; gap: 8px; align-items: center; flex-wrap: wrap;">
            <button class="btn btn-outline" style="padding: 3px 10px; font-size: 0.78rem;" onclick="setServerUrlFast('http://localhost:8765')">💻 填入本机 Python 默认地址 (http://localhost:8765)</button>
          </div>
          <div style="display: flex; gap: 10px; flex-wrap: wrap; margin-bottom: 8px;">
            <button id="btn-cloud-twoway" class="btn btn-primary" onclick="triggerWebCloudSync('two_way')">🔄 立即双向云同步</button>
            <button class="btn btn-secondary" onclick="triggerWebCloudSync('pull')">📥 从云端拉取</button>
            <button class="btn btn-secondary" onclick="triggerWebCloudSync('push')">📤 推送至云端</button>
          </div>
        </div>

        <!-- 模式2: GitHub Gist -->
        <div id="cloud-gist-pane" style="display: none;">
          <div style="display: flex; gap: 10px; margin-bottom: 10px; flex-wrap: wrap;">
            <div style="flex: 2; min-width: 220px;">
              <label style="font-size: 0.8rem; font-weight: 600; color: #94A3B8;">GitHub Token：</label>
              <input type="password" id="cloud-gist-token" class="form-input" style="margin-top: 4px;" placeholder="ghp_xxxxxxxxxxxx">
            </div>
            <div style="flex: 1; min-width: 140px;">
              <label style="font-size: 0.8rem; font-weight: 600; color: #94A3B8;">Gist ID (可选)：</label>
              <input type="text" id="cloud-gist-id" class="form-input" style="margin-top: 4px;" placeholder="首次留空自动创建">
            </div>
          </div>
          <div style="display: flex; gap: 10px; flex-wrap: wrap; margin-bottom: 8px;">
            <button id="btn-gist-twoway" class="btn btn-primary" onclick="triggerWebGistSync('two_way')">🔄 Gist 立即双向同步</button>
          </div>
        </div>

        <!-- 自动同步选项 -->
        <div style="margin-top: 10px; padding-top: 10px; border-top: 1px solid rgba(255,255,255,0.08); font-size: 0.82rem; display: flex; gap: 16px; flex-wrap: wrap;">
          <label style="cursor: pointer;"><input type="checkbox" id="cloud-auto-load" onchange="saveCloudSettings()"> 打开网页时自动从云端对齐</label>
          <label style="cursor: pointer;"><input type="checkbox" id="cloud-auto-save" onchange="saveCloudSettings()"> 复习完成或录入新词后自动推送到云端</label>
        </div>

        <!-- 反馈提示 -->
        <div id="cloud-feedback-msg" style="margin-top: 8px; font-size: 0.82rem; color: #94A3B8;"></div>
      </div>

      <!-- 📦 进度一致性自检：内置快照合并 + 重复词清理 -->
      <div class="card" style="margin-bottom: 18px; border: 1px solid rgba(56,189,248,0.35);">
        <h3 style="font-size: 1.05rem; font-weight: 700; margin-bottom: 6px;">📦 本地进度一致性自检</h3>
        <p style="font-size: 0.85rem; color: var(--text-muted); margin-bottom: 10px;">
          每次打开网页，系统都会自动把「新版网页内置的最新进度」合并进本机数据，并清理重复单词。合并规则：只补不删、只升级不降级。
        </p>
        <div id="builtin-snapshot-info" style="background: rgba(30, 41, 59, 0.8); border-radius: 8px; padding: 10px 14px; font-size: 0.82rem; line-height: 1.7; color: #CBD5E1; margin-bottom: 12px;">
          📦 正在读取内置进度快照…
        </div>
        <div style="display: flex; gap: 10px; flex-wrap: wrap;">
          <button class="btn btn-primary" onclick="mergeBuiltinSnapshotAction()">🔁 立即合并内置最新进度</button>
          <button class="btn btn-secondary" onclick="dedupeWordsAction(false)">🧹 立即清理重复单词</button>
        </div>
      </div>

      <div class="card" style="margin-bottom: 18px;">
        <h3 style="font-size: 1.05rem; font-weight: 700; margin-bottom: 6px;">📤 导出同步数据包（从本网页迁出）</h3>
        <p style="font-size: 0.85rem; color: var(--text-muted); margin-bottom: 14px;">
          导出包含全部自定义词单、所有单词、记忆稳定性参数与复习历史的通用数据包。与 Mac/Windows 客户端 100% 互通！
        </p>
        <div style="display: flex; gap: 10px; flex-wrap: wrap;">
          <button class="btn btn-primary" onclick="exportSyncFileAction()">💾 导出为同步文件 (.json)</button>
          <button class="btn btn-secondary" onclick="copySyncDataAction()">📋 复制完整同步文本</button>
        </div>
      </div>

      <div class="card" style="margin-bottom: 18px;">
        <h3 style="font-size: 1.05rem; font-weight: 700; margin-bottom: 6px;">📥 导入同步数据包（同步到本网页）</h3>
        <p style="font-size: 0.85rem; color: var(--text-muted); margin-bottom: 14px;">
          可导入从 Mac、Windows 电脑或其他手机上导出的同步包，系统将自动智能合并词单与背诵进度。
        </p>

        <div style="margin-bottom: 14px;">
          <label style="font-size: 0.85rem; margin-right: 8px; font-weight: 600;">同步策略：</label>
          <label style="margin-right: 14px; font-size: 0.85rem; cursor: pointer;">
            <input type="radio" name="sync-mode" value="merge" checked> ✨ 智能双向合并 (推荐)
          </label>
          <label style="font-size: 0.85rem; cursor: pointer;">
            <input type="radio" name="sync-mode" value="overwrite"> ⚠️ 完全覆盖当前数据
          </label>
        </div>

        <div style="display: flex; gap: 10px; flex-wrap: wrap;">
          <input type="file" id="import-file-input" accept=".json" style="display: none;" onchange="handleFileImport(event)">
          <button class="btn btn-success" onclick="document.getElementById('import-file-input').click()">📂 选择同步文件并同步</button>
          <button class="btn btn-secondary" onclick="importFromClipboardAction()">📋 从剪贴板文本导入</button>
        </div>
      </div>

      <div class="card" style="background-color: #172033;">
        <h4 style="font-size: 0.95rem; font-weight: 600; color: #FCA5A5; margin-bottom: 6px;">危险操作区</h4>
        <p style="font-size: 0.85rem; color: var(--text-muted); margin-bottom: 10px;">重置为预置词库（若曾有录入，重置前请先导出备份）。</p>
        <button class="btn btn-danger" onclick="resetToInitialDataAction()">⚠️ 重置为初始默认词库</button>
      </div>
    </section>

  </main>

  <!-- ── 移动端底部导航栏 ── -->
  <nav id="mobile-bottom-nav">
    <div class="mobile-nav-item active" data-tab="add"><span class="icon">📥</span><span>录入</span></div>
    <div class="mobile-nav-item" data-tab="photo"><span class="icon">📷</span><span>拍照</span></div>
    <div class="mobile-nav-item" data-tab="review"><span class="icon">📅</span><span>复习</span></div>
    <div class="mobile-nav-item" data-tab="sweep"><span class="icon">📖</span><span>拉通</span></div>
    <div class="mobile-nav-item" data-tab="quiz"><span class="icon">🧠</span><span>自测</span></div>
    <div class="mobile-nav-item" data-tab="words"><span class="icon">📚</span><span>词库</span></div>
    <div class="mobile-nav-item" data-tab="stats"><span class="icon">📊</span><span>统计</span></div>
    <div class="mobile-nav-item" data-tab="sync"><span class="icon">🔄</span><span>同步</span></div>
  </nav>

  <!-- ── 弹窗：新建词单 ── -->
  <div id="modal-new-list" class="modal-overlay">
    <div class="modal-card">
      <h3 style="font-size: 1.15rem; font-weight: 700; margin-bottom: 16px;">➕ 创建新词单 / 单词书</h3>
      <div style="margin-bottom: 14px;">
        <label style="display: block; font-size: 0.85rem; margin-bottom: 6px;">词单名称 *</label>
        <input type="text" id="new-list-name" class="input-field" placeholder="如：🔥 独立写作高频词 / TPO听力错词">
      </div>
      <div style="margin-bottom: 14px;">
        <label style="display: block; font-size: 0.85rem; margin-bottom: 6px;">备注说明（可选）</label>
        <input type="text" id="new-list-desc" class="input-field" placeholder="如：针对强化听力讲座篇章生词">
      </div>
      <div style="margin-bottom: 20px;">
        <label style="display: block; font-size: 0.85rem; margin-bottom: 8px;">标识主题色</label>
        <div id="color-picker-group" style="display: flex; gap: 8px;"></div>
      </div>
      <div style="display: flex; gap: 10px;">
        <button class="btn btn-primary" style="flex: 1;" onclick="saveNewListAction()">确认创建</button>
        <button class="btn btn-outline" onclick="closeModals()">取消</button>
      </div>
    </div>
  </div>

  <!-- ── 弹窗：重命名词单 ── -->
  <div id="modal-rename-list" class="modal-overlay">
    <div class="modal-card">
      <h3 style="font-size: 1.15rem; font-weight: 700; margin-bottom: 16px;">✏️ 编辑 / 重命名词单</h3>
      <div style="margin-bottom: 14px;">
        <label style="display: block; font-size: 0.85rem; margin-bottom: 6px;">词单名称 *</label>
        <input type="text" id="rename-list-name" class="input-field">
      </div>
      <div style="margin-bottom: 14px;">
        <label style="display: block; font-size: 0.85rem; margin-bottom: 6px;">备注说明</label>
        <input type="text" id="rename-list-desc" class="input-field">
      </div>
      <div style="display: flex; gap: 10px;">
        <button class="btn btn-primary" style="flex: 1;" onclick="saveRenameListAction()">保存修改</button>
        <button class="btn btn-outline" onclick="closeModals()">取消</button>
      </div>
    </div>
  </div>

  <!-- ── 弹窗：编辑单词 ── -->
  <div id="modal-edit-word" class="modal-overlay">
    <div class="modal-card">
      <h3 id="edit-word-title" style="font-size: 1.15rem; font-weight: 700; margin-bottom: 16px;">✏️ 编辑单词</h3>
      <input type="hidden" id="edit-word-id">
      <div style="margin-bottom: 12px;">
        <label style="display: block; font-size: 0.85rem; margin-bottom: 6px;">所属词单</label>
        <select id="edit-word-list" class="input-field"></select>
      </div>
      <div style="margin-bottom: 12px;">
        <label style="display: block; font-size: 0.85rem; margin-bottom: 6px;">音标</label>
        <input type="text" id="edit-word-phonetic" class="input-field">
      </div>
      <div style="margin-bottom: 12px;">
        <label style="display: block; font-size: 0.85rem; margin-bottom: 6px;">中文释义 *</label>
        <input type="text" id="edit-word-meaning" class="input-field">
      </div>
      <div style="margin-bottom: 12px;">
        <label style="display: block; font-size: 0.85rem; margin-bottom: 6px;">例句</label>
        <input type="text" id="edit-word-example" class="input-field">
      </div>
      <div style="margin-bottom: 20px;">
        <label style="display: block; font-size: 0.85rem; margin-bottom: 6px;">个人备注 / 出处</label>
        <input type="text" id="edit-word-note" class="input-field">
      </div>
      <div style="display: flex; gap: 10px;">
        <button class="btn btn-primary" style="flex: 1;" onclick="saveEditWordAction()">保存更改</button>
        <button class="btn btn-outline" onclick="closeModals()">取消</button>
      </div>
    </div>
  </div>

  <!-- ── 弹窗：批量导入（Excel / Word / CSV / 文本） ── -->
  <div id="modal-batch-import" class="modal-overlay">
    <div class="modal-card" style="max-width: 800px;">
      <h3 style="font-size: 1.15rem; font-weight: 700; margin-bottom: 4px;">📋 批量导入单词 · Excel / Word / CSV</h3>
      <div style="font-size: 0.72rem; color: #64748B; margin-bottom: 12px;">
        网页版构建时间：<span id="import-build-stamp">--</span> · 支持 .xlsx .xls .csv .tsv .txt .docx .doc .json
      </div>
      <div style="margin-bottom: 12px;">
        <label style="font-size: 0.85rem; margin-right: 8px;">导入到词单：</label>
        <select id="import-target-list" class="input-field" style="width: 240px; padding: 6px 10px;"></select>
      </div>

      <div id="import-drop-zone" style="border: 2px dashed rgba(56,189,248,0.45); border-radius: 10px; padding: 20px 14px; text-align: center; font-size: 0.9rem; color: #CBD5E1; cursor: pointer; transition: all .15s;">
        📥 把文件拖到这里，或点击选择文件<br>
        <span style="color: var(--text-muted); font-size: 0.78rem; line-height: 1.9;">
          Excel <b>.xlsx / .xls</b> · Word <b>.docx</b> · 表格 <b>.csv / .tsv / .txt</b> · 同步包 <b>.json</b><br>
          老格式 <code>.xls</code> / <code>.doc</code> 也欢迎，会尝试自动解析；失败时会提示你另存为新格式
        </span>
      </div>
      <input type="file" id="import-batch-file-input" accept=".xlsx,.xlsm,.xls,.csv,.tsv,.txt,.docx,.doc,.json" style="display: none;">

      <details style="margin-top: 12px;">
        <summary style="cursor: pointer; font-size: 0.85rem; color: #60A5FA;">✍️ 也可以直接粘贴文本导入（每行一个单词）</summary>
        <textarea id="import-text-area" class="input-field" rows="6" placeholder="每行一个单词，例如：&#10;ubiquitous&#10;ephemeral, 短暂的&#10;photosynthesis, /ˌfoʊtoʊˈsɪnθəsɪs/, 光合作用" style="margin-top: 8px; font-family: monospace; font-size: 0.85rem; resize: vertical;"></textarea>
        <button class="btn btn-secondary" style="margin-top: 8px; font-size: 0.82rem;" onclick="parsePastedImportText()">🔍 解析粘贴的内容</button>
      </details>

      <div id="import-preview-area" style="margin-top: 14px;"></div>
      <div id="import-error-box" style="display: none; margin-top: 12px; padding: 10px 12px; border-radius: 8px; background: rgba(239,68,68,0.12); border: 1px solid rgba(239,68,68,0.4); color: #FCA5A5; font-size: 0.8rem; line-height: 1.7; word-break: break-all;"></div>

      <div style="display: flex; gap: 10px; margin-top: 14px;">
        <button class="btn btn-primary" style="flex: 1;" id="import-confirm-btn" onclick="confirmImportEntries()" disabled>确认导入</button>
        <button class="btn btn-outline" onclick="closeModals()">取消</button>
      </div>
    </div>
  </div>

  <!-- Toast 消息容器 -->
  <div id="toast"></div>

  <!-- ── 脚本业务逻辑 ── -->
  <script>
    // 1. 内置默认数据库
    const INITIAL_DEFAULT_DATA = {default_data_json};

    const STORAGE_KEY = 'TOEFL_VOCAB_WEB_V2';
    const BACKUP_KEY = 'TOEFL_VOCAB_BACKUP_V2';
    // 内置快照元信息 / 合并标记 / 已删除单词墓碑（保证多次更新网页进度不打架）
    const BUILTIN_SNAPSHOT_META = {snapshot_meta_json};
    const BUILTIN_MERGED_KEY = 'TOEFL_VOCAB_BUILTIN_MERGED';
    const DELETED_WORDS_KEY = 'TOEFL_VOCAB_DELETED_WORDS';
    const APP_BUILD_STAMP = '{BUILD_STAMP}';
    const THEME_COLORS = ['#2563EB', '#EA580C', '#0D9488', '#7C3AED', '#DB2777', '#D97706', '#059669'];

    // 内存数据
    let appData = {{
      word_lists: [],
      words: []
    }};

    // 状态字典
    const STATE_LABELS = {{ 0: "🆕 新词", 1: "📖 学习中", 2: "✅ 复习", 3: "🔄 重学" }};
    const STATE_COLORS = {{ 0: "#8B5CF6", 1: "#F59E0B", 2: "#10B981", 3: "#EF4444" }};

    // ── 安全防吞字与 XSS 转义函数 ──
    function esc(str) {{
      if (str === null || str === undefined) return '';
      return String(str)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#039;');
    }}

    // ── 单调自增唯一 ID 生成器（彻底消除重复 ID 导致的数据覆盖 bug） ──
    function generateUniqueId() {{
      let maxId = 1000;
      if (appData.words && appData.words.length > 0) {{
        for (let i = 0; i < appData.words.length; i++) {{
          const wid = Number(appData.words[i].id);
          if (!isNaN(wid) && wid > maxId) maxId = wid;
        }}
      }}
      return Math.max(Date.now(), maxId + 1);
    }}

    // ── IndexedDB 数据库层封装（高持久化防被清理） ──
    const DB_NAME = 'ToeflVocabStorage';
    const DB_VERSION = 1;
    const STORE_NAME = 'main_data';
    let indexedDbDisabled = false;   // file:// 等环境下 IndexedDB 不可用时置位，后续不再重试

    function openIndexedDB() {{
      return new Promise((resolve) => {{
        if (!window.indexedDB || indexedDbDisabled) {{
          resolve(null);
          return;
        }}
        // ⚠️ 在 file:// 直接双击打开的场景下，Chrome 的 IndexedDB 可能永远不回调，
        // 这里加超时兜底，确保初始化流程绝不会被 IndexedDB 卡住（最坏情况降级为 LocalStorage）
        let settled = false;
        const finish = (value) => {{
          if (settled) return;
          settled = true;
          resolve(value);
        }};
        try {{
          const req = indexedDB.open(DB_NAME, DB_VERSION);
          req.onupgradeneeded = (e) => {{
            const db = e.target.result;
            if (!db.objectStoreNames.contains(STORE_NAME)) {{
              db.createObjectStore(STORE_NAME);
            }}
          }};
          req.onsuccess = (e) => finish(e.target.result);
          req.onerror = () => finish(null);
          req.onblocked = () => finish(null);
          setTimeout(() => {{ indexedDbDisabled = true; finish(null); }}, 2000);
        }} catch (e) {{
          indexedDbDisabled = true;
          finish(null);
        }}
      }});
    }}

    async function saveToIndexedDB(data) {{
      try {{
        const db = await openIndexedDB();
        if (!db) return;
        const tx = db.transaction(STORE_NAME, 'readwrite');
        const store = tx.objectStore(STORE_NAME);
        store.put(data, 'snapshot');
      }} catch(e) {{
        console.warn("IndexedDB 存储异常", e);
      }}
    }}

    async function loadFromIndexedDB() {{
      try {{
        const db = await openIndexedDB();
        if (!db) return null;
        return new Promise((resolve) => {{
          const tx = db.transaction(STORE_NAME, 'readonly');
          const store = tx.objectStore(STORE_NAME);
          const req = store.get('snapshot');
          let settled = false;
          const finish = (value) => {{
            if (settled) return;
            settled = true;
            resolve(value);
          }};
          req.onsuccess = () => finish(req.result || null);
          req.onerror = () => finish(null);
          setTimeout(() => finish(null), 2000);
        }});
      }} catch(e) {{
        return null;
      }}
    }}

{PHOTO_CORE_JS}

{WEB_STATE_MERGE_JS}

{WEB_IMPORT_JS}

{PHOTO_UI_JS}

    // ── 初始化与防丢失加载机制 ──
    async function initApp() {{
      initAudioVoices();
      await loadDataDefensive();
      setupNavigation();
      setupColorPicker();
      setupKeyboardShortcuts();
      setupImportDropZone();
      updateAllListDropdowns();
      updateGlobalStatusBar();
      initCloudSettingsUI();
      renderBuiltinSnapshotInfo();
      renderBuildStamp();
      switchTab(location.hash === '#photo' ? 'photo' : 'add');

      // 启动时把「新版网页内置进度」+「重复词清理」的结果如实汇报给用户
      const mergedStats = pendingBuiltinMergeStats;
      if (mergedStats && (mergedStats.words_added || mergedStats.schedules_updated || mergedStats.reviews_added)) {{
        showToast(`✅ 已同步新版网页内置进度：${{builtinMergeStatsText(mergedStats)}}`, 4200);
      }}

      // 启动时自动从云端对齐
      const ccfg = getCloudSettings();
      if (ccfg.autoLoad) {{
        setTimeout(() => {{
          if (ccfg.mode === 'gist' && ccfg.gistToken) {{
            triggerWebGistSync('two_way', true);
          }} else if (ccfg.serverUrl) {{
            triggerWebCloudSync('two_way', true);
          }}
        }}, 800);
      }}
    }}

    // 防御式三级数据加载策略
    async function loadDataDefensive() {{
      let loaded = null;

      // 级别 1: LocalStorage 读取
      const raw = localStorage.getItem(STORAGE_KEY);
      if (raw) {{
        try {{
          const parsed = JSON.parse(raw);
          if (parsed && Array.isArray(parsed.words) && (parsed.words.length > 0 || parsed.photo_state?.batches?.length > 0)) {{
            loaded = parsed;
          }}
        }} catch(e) {{
          console.error("LocalStorage 读取异常", e);
        }}
      }}

      // 级别 2: 若 LocalStorage 为空或损坏，尝试从 IndexedDB 灾备恢复
      if (!loaded) {{
        const idbData = await loadFromIndexedDB();
        if (idbData && Array.isArray(idbData.words) && (idbData.words.length > 0 || idbData.photo_state?.batches?.length > 0)) {{
          console.log("✅ 成功从 IndexedDB 灾难备份中恢复词库！");
          loaded = idbData;
        }}
      }}

      // 级别 3: 若依然无数据，尝试读取 LocalStorage 快照备份
      if (!loaded) {{
        const backupRaw = localStorage.getItem(BACKUP_KEY);
        if (backupRaw) {{
          try {{
            const backupParsed = JSON.parse(backupRaw);
            if (backupParsed && Array.isArray(backupParsed.words) && (backupParsed.words.length > 0 || backupParsed.photo_state?.batches?.length > 0)) {{
              console.log("✅ 成功从快照备份中恢复词库！");
              loaded = backupParsed;
            }}
          }} catch(e) {{}}
        }}
      }}

      // 级别 4: 全新启动，加载预置的 78 词默认词库
      if (!loaded) {{
        loaded = JSON.parse(JSON.stringify(INITIAL_DEFAULT_DATA));
      }}

      appData = loaded;
      PhotoCore.ensure(appData);

      // 健壮性校验
      if (!Array.isArray(appData.word_lists) || appData.word_lists.length === 0) {{
        appData.word_lists = [
          {{ id: 1, name: "📘 托福核心词汇", description: "官方核心高频词汇", color: "#2563EB", created_at: new Date().toISOString() }}
        ];
      }}
      if (!Array.isArray(appData.words)) {{
        appData.words = [];
      }}

      // 保证每个单词都有唯一 ID（防御预置数据或旧数据丢失 id 字段）
      let curMaxId = 1000;
      for (let i = 0; i < appData.words.length; i++) {{
        if (appData.words[i].id) {{
          const nid = Number(appData.words[i].id);
          if (!isNaN(nid) && nid > curMaxId) curMaxId = nid;
        }}
      }}
      for (let i = 0; i < appData.words.length; i++) {{
        if (!appData.words[i].id) {{
          appData.words[i].id = ++curMaxId;
        }}
      }}

      // 级别 5: 自动合并「新版网页内置的最新进度快照」——只补不删，旧进度不会被覆盖
      pendingBuiltinMergeStats = mergeBuiltinSnapshot(false);

      // 级别 6: 自动清理重复单词（同一单词跨设备 / 多次导入后可能重复）
      const dedupeStats = dedupeWords(appData);
      if (dedupeStats.duplicates_removed > 0) {{
        console.log('🧹 启动自动去重：' + dedupeStatsText(dedupeStats));
        setTimeout(() => showToast(`🧹 已自动清理 ${{dedupeStats.duplicates_removed}} 条重复单词`, 4000), 500);
      }}

      saveData();
    }}

    // 双重存储与自动快照
    function saveData() {{
      try {{
        // 1. 同步保存至 LocalStorage
        localStorage.setItem(STORAGE_KEY, JSON.stringify(appData));

        // 2. 若数据完整有效，建立安全快照备份
        if (appData.words.length > 0) {{
          localStorage.setItem(BACKUP_KEY, JSON.stringify(appData));
        }}

        // 3. 异步持久化到 IndexedDB
        saveToIndexedDB(appData);

      }} catch(e) {{
        console.error("持久化存储异常", e);
        showToast("⚠️ 存储空间报警，请及时导出备份！");
      }}
      updateGlobalStatusBar();
    }}

    function showToast(msg, duration = 2500) {{
      const t = document.getElementById('toast');
      t.textContent = msg;
      t.classList.add('show');
      setTimeout(() => t.classList.remove('show'), duration);
    }}

    // ── 🔊 高保真真人发音引擎（优先官方真人美音/英音原声MP3，兜底严格纯英TTS） ──
    let globalAudioPlayer = null;
    let cachedVoices = [];

    function initAudioVoices() {{
      if ('speechSynthesis' in window) {{
        const refreshVoices = () => {{
          try {{
            cachedVoices = window.speechSynthesis.getVoices() || [];
          }} catch(e) {{}}
        }};
        refreshVoices();
        if (window.speechSynthesis.onvoiceschanged !== undefined) {{
          window.speechSynthesis.onvoiceschanged = refreshVoices;
        }}
      }}
    }}

    function getAudioPlayer() {{
      if (!globalAudioPlayer) {{
        globalAudioPlayer = new Audio();
        globalAudioPlayer.preload = 'auto';
      }}
      return globalAudioPlayer;
    }}

    function speakWord(word) {{
      if (!word) return;
      const cleanWord = word.trim();
      if (!cleanWord) return;

      // 音频源1：有道官方美音真人原声（标清/高清 MP3，托福听力纯正标准人声）
      const usAudioUrl = `https://dict.youdao.com/dictvoice?audio=${{encodeURIComponent(cleanWord)}}&type=2`;
      // 音频源2：有道官方英音真人原声（备用人声 MP3）
      const ukAudioUrl = `https://dict.youdao.com/dictvoice?audio=${{encodeURIComponent(cleanWord)}}&type=1`;
      // 音频源3：Google Translate 语音流
      const googleAudioUrl = `https://translate.google.com/translate_tts?ie=UTF-8&client=tw-ob&tl=en&q=${{encodeURIComponent(cleanWord)}}`;

      const player = getAudioPlayer();
      try {{
        player.pause();
      }} catch(e) {{}}

      let currentSourceIdx = 0;
      const sources = [usAudioUrl, ukAudioUrl, googleAudioUrl];

      const tryFallbackTTS = () => {{
        playLocalSpeechSynthesis(cleanWord);
      }};

      player.onerror = () => {{
        currentSourceIdx++;
        if (currentSourceIdx < sources.length) {{
          player.src = sources[currentSourceIdx];
          const p = player.play();
          if (p && p.catch) p.catch(() => {{ player.onerror(); }});
        }} else {{
          tryFallbackTTS();
        }}
      }};

      try {{
        player.src = sources[0];
        const playPromise = player.play();
        if (playPromise !== undefined) {{
          playPromise.catch(err => {{
            // 若网络加载受阻或被自动播放策略临时拦截，平滑触发重试/TTS
            player.onerror();
          }});
        }}
      }} catch (err) {{
        tryFallbackTTS();
      }}
    }}

    function playLocalSpeechSynthesis(cleanWord) {{
      if (!('speechSynthesis' in window)) return;
      try {{
        if (window.speechSynthesis.paused) {{
          window.speechSynthesis.resume();
        }}

        if (!cachedVoices.length) {{
          cachedVoices = window.speechSynthesis.getVoices() || [];
        }}

        // 严格挑选真实英文发音人，严禁中文语音库生硬读英文字母！
        const enVoice = cachedVoices.find(v => v.lang === 'en-US') ||
                        cachedVoices.find(v => v.lang === 'en-GB') ||
                        cachedVoices.find(v => v.lang && v.lang.startsWith('en')) ||
                        cachedVoices.find(v => v.name && v.name.toLowerCase().includes('english'));

        const utter = new SpeechSynthesisUtterance(cleanWord);
        if (enVoice) {{
          utter.voice = enVoice;
          utter.lang = enVoice.lang;
        }} else {{
          utter.lang = 'en-US';
        }}
        utter.rate = 0.92;
        utter.pitch = 1.0;
        window.speechSynthesis.speak(utter);
      }} catch(e) {{
        console.warn('SpeechSynthesis playback failed:', e);
      }}
    }}

    // ── 导航与选项卡切换 ──
    function setupNavigation() {{
      document.querySelectorAll('.nav-item').forEach(el => {{
        el.addEventListener('click', () => switchTab(el.dataset.tab));
      }});
      document.querySelectorAll('.mobile-nav-item').forEach(el => {{
        el.addEventListener('click', () => switchTab(el.dataset.tab));
      }});
    }}

    function switchTab(tabKey) {{
      document.querySelectorAll('.nav-item').forEach(el => {{
        el.classList.toggle('active', el.dataset.tab === tabKey);
      }});
      document.querySelectorAll('.mobile-nav-item').forEach(el => {{
        el.classList.toggle('active', el.dataset.tab === tabKey);
      }});
      document.querySelectorAll('.view-page').forEach(el => {{
        el.classList.toggle('active', el.id === `view-${{tabKey}}`);
      }});

      if (tabKey === 'photo') {{
        PhotoUI.init();
      }} else if (tabKey === 'review') {{
        loadReviewQueue();
      }} else if (tabKey === 'sweep') {{
        initSweepOnEnter();
      }} else if (tabKey === 'quiz') {{
        nextQuizQuestion();
      }} else if (tabKey === 'words') {{
        renderWordListTable();
      }} else if (tabKey === 'stats') {{
        renderStatsView();
      }}
    }}

    function getEndOfTodayIso() {{
      const d = new Date();
      d.setHours(23, 59, 59, 999);
      return d.toISOString();
    }}

    function updateGlobalStatusBar() {{
      const total = appData.words.length;
      const endOfTodayIso = getEndOfTodayIso();
      const dueCount = appData.words.filter(w => !w.photo_pending && (!w.schedule || !w.schedule.due || w.schedule.due <= endOfTodayIso)).length;
      const statusText = `总词数：${{total}} 个 (${{appData.word_lists.length}} 个词单)<br>今日待复习：${{dueCount}} 个`;
      document.getElementById('sidebar-status').innerHTML = statusText;
    }}

    function updateAllListDropdowns() {{
      const selects = [
        document.getElementById('add-list-select'),
        document.getElementById('rev-list-filter'),
        document.getElementById('quiz-list-filter'),
        document.getElementById('words-list-filter'),
        document.getElementById('edit-word-list'),
        document.getElementById('import-target-list'),
        document.getElementById('sweep-list-filter')
      ];

      const lists = appData.word_lists;

      selects.forEach(sel => {{
        if (!sel) return;
        const prevVal = sel.value;
        sel.innerHTML = '';

        const isFilter = sel.id.includes('filter');
        if (isFilter) {{
          const optAll = document.createElement('option');
          optAll.value = "all";
          optAll.textContent = "📚 全部词单";
          sel.appendChild(optAll);
        }}

        lists.forEach(l => {{
          const opt = document.createElement('option');
          opt.value = l.id;
          opt.textContent = l.name;
          sel.appendChild(opt);
        }});

        if (prevVal && [...sel.options].some(o => o.value == prevVal)) {{
          sel.value = prevVal;
        }}
      }});
    }}

    // ── 1. 录入单词模块（高健壮性有道权威中文词典 JSONP 引擎，零跨域阻断） ──
    function fetchYoudaoChineseMeaning(word) {{
      return new Promise((resolve) => {{
        const cbName = 'youdao_cb_' + Date.now() + '_' + Math.floor(Math.random() * 10000);
        const script = document.createElement('script');
        let done = false;

        const timer = setTimeout(() => {{
          if (!done) {{
            done = true;
            cleanup();
            resolve(null);
          }}
        }}, 3500);

        function cleanup() {{
          try {{
            delete window[cbName];
            if (script.parentNode) script.parentNode.removeChild(script);
          }} catch(e) {{}}
        }}

        window[cbName] = function(data) {{
          if (done) return;
          done = true;
          clearTimeout(timer);
          cleanup();
          try {{
            if (data && data.data && Array.isArray(data.data.entries) && data.data.entries.length > 0) {{
              const entry = data.data.entries[0];
              if (entry.explain) {{
                resolve(entry.explain);
                return;
              }}
            }}
          }} catch(e) {{}}
          resolve(null);
        }};

        script.onerror = function() {{
          if (!done) {{
            done = true;
            clearTimeout(timer);
            cleanup();
            resolve(null);
          }}
        }};

        script.src = 'https://dict.youdao.com/suggest?q=' + encodeURIComponent(word) + '&num=1&doctype=json&callback=' + cbName;
        document.body.appendChild(script);
      }});
    }}

    async function fetchPhonetic(word) {{
      // 优先从已有词库查找音标
      const local = appData.words.find(w => w.word.toLowerCase() === word.toLowerCase() && w.phonetic);
      if (local) return local.phonetic;

      // 尝试从国际词典快速获取音标（1.5秒超时防阻断）
      try {{
        const controller = new AbortController();
        const tId = setTimeout(() => controller.abort(), 1500);
        const resp = await fetch('https://api.dictionaryapi.dev/api/v2/entries/en/' + encodeURIComponent(word), {{
          signal: controller.signal
        }});
        clearTimeout(tId);
        if (resp.ok) {{
          const data = await resp.json();
          if (Array.isArray(data) && data.length > 0) {{
            const entry = data[0];
            return entry.phonetic || (entry.phonetics && entry.phonetics.find(p => p.text)?.text) || '';
          }}
        }}
      }} catch(e) {{}}
      return '';
    }}

    async function lookupWordAction() {{
      const wordInput = document.getElementById('add-word-input');
      const w = wordInput.value.trim().toLowerCase();
      if (!w) {{
        showToast('⚠️ 请先输入英文单词');
        wordInput.focus();
        return;
      }}

      const btn = document.getElementById('btn-lookup');
      btn.textContent = '⏳ 查询中...';
      btn.disabled = true;

      try {{
        // 并行获取权威中文释义与音标
        const [chineseMeaning, phonetic] = await Promise.all([
          fetchYoudaoChineseMeaning(w),
          fetchPhonetic(w)
        ]);

        let filled = false;

        if (chineseMeaning) {{
          document.getElementById('add-meaning-input').value = chineseMeaning;
          filled = true;
        }}

        if (phonetic && !document.getElementById('add-phonetic-input').value) {{
          document.getElementById('add-phonetic-input').value = phonetic;
        }}

        if (filled) {{
          showToast(`✅ 权威中文释义获取成功！`);
        }} else {{
          // 本地词库同名词回退
          const localMatch = appData.words.find(x => x.word.toLowerCase() === w && x.meaning);
          if (localMatch) {{
            document.getElementById('add-meaning-input').value = localMatch.meaning;
            if (localMatch.phonetic && !document.getElementById('add-phonetic-input').value) {{
              document.getElementById('add-phonetic-input').value = localMatch.phonetic;
            }}
            showToast(`✅ 从已有词库自动匹配释义！`);
          }} else {{
            showToast('⚠️ 未检索到释义，请直接在此手动键入释义', 3000);
          }}
        }}
      }} catch(e) {{
        console.error('查词异常', e);
        showToast('⚠️ 查词服务超时，请直接手动键入释义');
      }} finally {{
        btn.textContent = '🔍 查释义';
        btn.disabled = false;
        speakWord(w);
      }}
    }}

    function saveNewWordAction() {{
      const word = document.getElementById('add-word-input').value.trim();
      if (!word) {{
        showToast('⚠️ 请输入英文单词！');
        document.getElementById('add-word-input').focus();
        return;
      }}

      // 严格去重（大小写不敏感）
      if (appData.words.some(w => w.word.trim().toLowerCase() === word.toLowerCase())) {{
        showToast(`⚠️ 单词「${{word}}」已在词库中！`);
        return;
      }}

      const listId = parseInt(document.getElementById('add-list-select').value) || appData.word_lists[0].id;
      const phonetic = document.getElementById('add-phonetic-input').value.trim();
      const meaning = document.getElementById('add-meaning-input').value.trim();
      const example = document.getElementById('add-example-input').value.trim();
      const note = document.getElementById('add-note-input').value.trim();

      const newWord = {{
        id: generateUniqueId(),
        word: word,
        phonetic: phonetic,
        meaning: meaning || "（暂无释义）",
        example: example,
        note: note,
        list_id: listId,
        created_at: new Date().toISOString(),
        schedule: {{
          due: new Date().toISOString(),
          stability: 0,
          difficulty: 5,
          elapsed_days: 0,
          scheduled_days: 1,
          reps: 0,
          lapses: 0,
          state: 0
        }},
        reviews: []
      }};

      appData.words.unshift(newWord);
      saveData();
      triggerAutoCloudPush();
      showToast(`✅ 已存入词单：${{word}}`);
      clearAddForm();
      document.getElementById('add-word-input').focus();
    }}

    function clearAddForm() {{
      document.getElementById('add-word-input').value = '';
      document.getElementById('add-phonetic-input').value = '';
      document.getElementById('add-meaning-input').value = '';
      document.getElementById('add-example-input').value = '';
      document.getElementById('add-note-input').value = '';
    }}

    // ── 2. 今日复习模块（实时测算背诵进度 + 动态闭环重学） ──
    let reviewQueue = [];
    let reviewIndex = 0;
    let reviewRevealed = false;
    let reviewInitialTotal = 0;
    let reviewMasteredIds = new Set();
    let reviewFirstAttempts = {{}}; // wordId -> first rating
    let reviewRelearnCounts = {{}}; // wordId -> fail count
    let reviewTotalReviews = 0;

    function isScheduleProtected(schedule) {{
      if (!schedule) return false;
      const state = schedule.state || 0;
      const stability = schedule.stability || 0;
      return state === 2 && (stability >= 7.0 || (schedule.reps || 0) >= 4);
    }}

    function isScheduleInLearning(schedule) {{
      if (!schedule) return true;
      const state = schedule.state || 0;
      const reps = schedule.reps || 0;
      return state === 0 || (state === 1 && reps < 3);
    }}

    function predictIntervals(schedule) {{
      const LEARNING_STEPS = [10, 60, 240];
      const intervals = [1, 2, 4, 7, 15, 30, 60];
      const reps = (schedule && schedule.reps) || 0;
      const state = (schedule && schedule.state) || 0;
      const stability = (schedule && schedule.stability) || 0;

      // 学习阶段（新词或未完成3轮学习步骤的词）：几分钟后、几十分钟后、几小时后、1天后
      if (isScheduleInLearning(schedule)) {{
        const againHint = '+10分钟';
        const hardStepIdx = Math.min(reps, LEARNING_STEPS.length - 1);
        const hardMins = LEARNING_STEPS[hardStepIdx];
        const hardHint = hardMins < 60 ? ('+' + hardMins + '分钟') : ('+' + Math.floor(hardMins / 60) + '小时');

        let goodHint, easyHint;
        const nextReps = reps + 1;
        if (nextReps >= LEARNING_STEPS.length) {{
          goodHint = '+1天';
          easyHint = '+1天';
        }} else {{
          const goodMins = LEARNING_STEPS[nextReps];
          goodHint = goodMins < 60 ? ('+' + goodMins + '分钟') : ('+' + Math.floor(goodMins / 60) + '小时');
          easyHint = goodHint;
        }}
        return {{
          1: againHint,
          2: hardHint,
          3: goodHint,
          4: easyHint
        }};
      }}

      // 长期掌握词保护提示（评 Again 时排在 1 小时后巩固，不重置）
      let againText = '+10分钟';
      if (isScheduleProtected(schedule)) {{
        againText = '+1小时';
      }}

      const idx2 = Math.min(reps, intervals.length - 1);
      const hardDays = Math.max(1, Math.floor(intervals[idx2] * 0.7));
      const idx3 = Math.min(reps + 1, intervals.length - 1);
      const goodDays = intervals[idx3];
      const idx4 = Math.min(reps + 2, intervals.length - 1);
      const easyDays = Math.ceil(intervals[idx4] * 1.5);
      return {{
        1: againText,
        2: '+' + hardDays + '天',
        3: '+' + goodDays + '天',
        4: '+' + easyDays + '天'
      }};
    }}

    function loadReviewQueue() {{
      const filter = document.getElementById('rev-list-filter').value;
      const nowIso = new Date().toISOString();
      const endOfTodayIso = getEndOfTodayIso();

      let pool = appData.words;
      if (filter !== 'all') {{
        const filterId = parseInt(filter);
        pool = pool.filter(w => w.list_id === filterId);
      }}

      // 筛选今日到期词（未安排的新词 + 今日23:59前到期的词，含分钟级到期词）
      const todayWords = pool.filter(w => !w.photo_pending && (!w.schedule || !w.schedule.due || w.schedule.due <= endOfTodayIso));

      // 已到期排在前面，稍后到期排在后面
      const dueNow = todayWords.filter(w => !w.schedule || !w.schedule.due || w.schedule.due <= nowIso);
      const dueLater = todayWords.filter(w => w.schedule && w.schedule.due && w.schedule.due > nowIso);
      reviewQueue = dueNow.concat(dueLater);

      reviewIndex = 0;
      reviewRevealed = false;
      reviewInitialTotal = reviewQueue.length;
      reviewMasteredIds = new Set();
      reviewFirstAttempts = {{}};
      reviewRelearnCounts = {{}};
      reviewTotalReviews = 0;

      renderCurrentReviewCard();
    }}

    function renderCurrentReviewCard() {{
      const container = document.getElementById('rev-card-container');
      const emptyBox = document.getElementById('rev-empty-state');
      const total = reviewQueue.length;

      // 实时计算各项关键测算指标
      const masteredCount = reviewMasteredIds.size;
      const initialTotal = Math.max(reviewInitialTotal, 1);
      const pendingCount = Math.max(0, reviewInitialTotal - masteredCount);
      let relearningCount = 0;
      for (const wid in reviewRelearnCounts) {{
        if (!reviewMasteredIds.has(parseInt(wid))) relearningCount++;
      }}

      const firstAttemptKeys = Object.keys(reviewFirstAttempts);
      let retentionStr = '100.0%';
      if (firstAttemptKeys.length > 0) {{
        let firstPassed = 0;
        for (const k of firstAttemptKeys) {{
          const r = reviewFirstAttempts[k];
          if (r === 3 || r === 4) firstPassed++;
        }}
        retentionStr = ((firstPassed / firstAttemptKeys.length) * 100).toFixed(1) + '%';
      }}

      // 更新实时看板
      const elPending = document.getElementById('rev-metric-pending');
      if (elPending) elPending.textContent = pendingCount;
      const elRelearning = document.getElementById('rev-metric-relearning');
      if (elRelearning) elRelearning.textContent = relearningCount;
      const elMastered = document.getElementById('rev-metric-mastered');
      if (elMastered) elMastered.textContent = masteredCount;
      const elRetention = document.getElementById('rev-metric-retention');
      if (elRetention) elRetention.textContent = retentionStr;

      // 进度条按掌握比例推进
      const progressPercent = reviewInitialTotal > 0 ? Math.min(100, (masteredCount / reviewInitialTotal) * 100) : 100;
      document.getElementById('rev-progress-counter').textContent = masteredCount + ' / ' + reviewInitialTotal;
      document.getElementById('rev-progress-bar').style.width = progressPercent + '%';

      // 检查队列是否全部完成
      if (reviewIndex >= total || total === 0) {{
        container.style.display = 'none';
        emptyBox.style.display = 'block';

        if (reviewInitialTotal > 0) {{
          document.getElementById('rev-empty-title').textContent = '🎉 今日背诵复习已圆满完成！';
          document.getElementById('rev-summary-stats').style.display = 'grid';
          document.getElementById('rev-summary-mastered').textContent = masteredCount;
          document.getElementById('rev-summary-retention').textContent = retentionStr;
          document.getElementById('rev-summary-relearned').textContent = Object.keys(reviewRelearnCounts).length;
          document.getElementById('rev-summary-rounds').textContent = reviewTotalReviews;
          document.getElementById('rev-empty-desc').textContent = '生词与遗忘词汇已在动态闭环中全部彻底掌握！';
          triggerAutoCloudPush();
        }} else {{
          document.getElementById('rev-empty-title').textContent = '今日无待复习词汇';
          document.getElementById('rev-summary-stats').style.display = 'none';
          document.getElementById('rev-empty-desc').textContent = '所选词单当前无待复习词汇，可切换词单或录入新词。';
        }}
        return;
      }}

      container.style.display = 'flex';
      emptyBox.style.display = 'none';

      const cur = reviewQueue[reviewIndex];

      const listObj = appData.word_lists.find(l => l.id === cur.list_id) || {{ name: '默认词单', color: '#2563EB' }};
      const badge = document.getElementById('rev-card-list-badge');
      badge.textContent = listObj.name;
      badge.style.backgroundColor = listObj.color;

      const stateVal = cur.schedule ? cur.schedule.state : 0;
      document.getElementById('rev-card-state-text').textContent = '· ' + (STATE_LABELS[stateVal] || '新词');

      // 动态重学巩固标签
      const relearnBadge = document.getElementById('rev-card-relearn-badge');
      if (cur.is_relearning) {{
        const cnt = cur.relearn_count || 1;
        relearnBadge.style.display = 'inline-block';
        relearnBadge.textContent = '⚡️ 动态巩固第 ' + cnt + ' 轮 · 上次未记牢';
      }} else {{
        relearnBadge.style.display = 'none';
      }}

      // 动态排期预测更新到按钮提示
      const intervals = predictIntervals(cur.schedule);
      document.getElementById('rev-btn-hint-1').textContent = '忘记了 (' + intervals[1] + ')';
      document.getElementById('rev-btn-hint-2').textContent = '模糊 (' + intervals[2] + ')';
      document.getElementById('rev-btn-hint-3').textContent = '记住了 (' + intervals[3] + ')';
      document.getElementById('rev-btn-hint-4').textContent = '很简单 (' + intervals[4] + ')';

      document.getElementById('rev-card-word').textContent = cur.word;
      document.getElementById('rev-card-phonetic').textContent = cur.phonetic || '';

      // 防吞字安全转义
      document.getElementById('rev-card-meaning').textContent = cur.meaning || '（暂无释义）';
      document.getElementById('rev-card-example').textContent = cur.example ? '📖 ' + cur.example : '';
      document.getElementById('rev-card-note').textContent = cur.note ? '📌 ' + cur.note : '';

      reviewRevealed = false;
      document.getElementById('rev-cover').style.display = 'block';
      document.getElementById('rev-answer').style.display = 'none';
      document.getElementById('rev-btn-reveal').style.display = 'block';
      document.getElementById('rev-rating-buttons').style.display = 'none';
    }}

    function revealReviewCard() {{
      if (reviewIndex >= reviewQueue.length) return;
      reviewRevealed = true;
      document.getElementById('rev-cover').style.display = 'none';
      document.getElementById('rev-answer').style.display = 'block';
      document.getElementById('rev-btn-reveal').style.display = 'none';
      document.getElementById('rev-rating-buttons').style.display = 'grid';

      speakCurrentWord();
    }}

    function speakCurrentWord() {{
      if (reviewIndex < reviewQueue.length) {{
        speakWord(reviewQueue[reviewIndex].word);
      }}
    }}

    function rateReviewCard(rating) {{
      if (!reviewRevealed) {{
        revealReviewCard();
        return;
      }}
      if (reviewIndex >= reviewQueue.length) return;

      const cur = reviewQueue[reviewIndex];
      const wordId = cur.id;
      reviewTotalReviews++;

      if (!(wordId in reviewFirstAttempts)) {{
        reviewFirstAttempts[wordId] = rating;
      }}

      const LEARNING_STEPS = [10, 60, 240]; // 10分钟 -> 1小时 -> 4小时
      const intervals = [1, 2, 4, 7, 15, 30, 60];
      if (!cur.schedule) cur.schedule = {{ reps: 0, lapses: 0, state: 0, stability: 0 }};

      const state = cur.schedule.state || 0;
      const reps = cur.schedule.reps || 0;
      const stability = cur.schedule.stability || 0;
      const lapses = cur.schedule.lapses || 0;
      const inLearning = isScheduleInLearning(cur.schedule);
      const isProtected = isScheduleProtected(cur.schedule);

      let dueTime = new Date();

      // ① 学习阶段（新词 / 尚未通过3轮学习步骤）：力求100%正确，反复强化
      if (inLearning) {{
        if (rating === 1) {{
          // Again: 回到第0步，10分钟后重学
          cur.schedule.lapses = lapses + 1;
          cur.schedule.reps = 0;
          cur.schedule.state = 1;
          cur.schedule.stability = 0.1;
          dueTime = new Date(Date.now() + LEARNING_STEPS[0] * 60 * 1000);

          const failCount = (reviewRelearnCounts[wordId] || 0) + 1;
          reviewRelearnCounts[wordId] = failCount;
          cur.is_relearning = true;
          cur.relearn_count = failCount;

          const remainingAfter = reviewQueue.length - (reviewIndex + 1);
          if (remainingAfter >= 3) {{
            reviewQueue.splice(reviewIndex + 1 + 3, 0, cur);
          }} else {{
            reviewQueue.push(cur);
          }}
          reviewIndex++;
        }} else if (rating === 2) {{
          // Hard: 停留在当前步骤，不增加 reps
          const stepIdx = Math.min(reps, LEARNING_STEPS.length - 1);
          dueTime = new Date(Date.now() + LEARNING_STEPS[stepIdx] * 60 * 1000);
          cur.schedule.state = 1;

          const failCount = (reviewRelearnCounts[wordId] || 0) + 1;
          reviewRelearnCounts[wordId] = failCount;
          cur.relearn_count = failCount;
          reviewQueue.push(cur);
          reviewIndex++;
        }} else {{
          // Good (3) 或 Easy (4): 推进学习步骤
          const newReps = reps + 1;
          cur.schedule.reps = newReps;

          if (newReps >= LEARNING_STEPS.length) {{
            // 完成所有学习步骤，毕业转入长期计划（第1天）
            cur.schedule.state = 2;
            cur.schedule.stability = 1.0;
            dueTime = new Date(Date.now() + 24 * 60 * 60 * 1000);
            reviewMasteredIds.add(wordId);
            reviewIndex++;
          }} else {{
            // 还在步骤中（10分钟/1小时/4小时）：排期并追加到队尾，本轮反复强化
            cur.schedule.state = 1;
            const nextMins = LEARNING_STEPS[newReps];
            dueTime = new Date(Date.now() + nextMins * 60 * 1000);
            reviewQueue.push(cur);
            reviewIndex++;
          }}
        }}
      }}
      // ② 长期掌握词受保护（state=2 且 stability>=7.0）：绝不覆盖或清零！
      else if (rating === 1 && isProtected) {{
        cur.schedule.lapses = lapses + 1;
        cur.schedule.stability = Math.max(3.5, stability * 0.7);
        cur.schedule.reps = reps + 1;
        cur.schedule.state = 2; // 保持掌握状态，绝不降为新词或抹掉！
        dueTime = new Date(Date.now() + 60 * 60 * 1000); // 1小时后快速巩固

        const failCount = (reviewRelearnCounts[wordId] || 0) + 1;
        reviewRelearnCounts[wordId] = failCount;
        cur.is_relearning = true;
        cur.relearn_count = failCount;

        const remainingAfter = reviewQueue.length - (reviewIndex + 1);
        if (remainingAfter >= 3) {{
          reviewQueue.splice(reviewIndex + 1 + 3, 0, cur);
        }} else {{
          reviewQueue.push(cur);
        }}
        reviewIndex++;
      }}
      // ③ 长期复习阶段正常计算
      else {{
        if (rating === 1) {{
          cur.schedule.lapses = lapses + 1;
          cur.schedule.reps = 0;
          cur.schedule.state = 3;
          cur.schedule.stability = 0.5;
          dueTime = new Date(Date.now() + 10 * 60 * 1000);

          const failCount = (reviewRelearnCounts[wordId] || 0) + 1;
          reviewRelearnCounts[wordId] = failCount;
          cur.is_relearning = true;
          cur.relearn_count = failCount;

          const remainingAfter = reviewQueue.length - (reviewIndex + 1);
          if (remainingAfter >= 3) {{
            reviewQueue.splice(reviewIndex + 1 + 3, 0, cur);
          }} else {{
            reviewQueue.push(cur);
          }}
          reviewIndex++;
        }} else if (rating === 2) {{
          cur.schedule.reps = reps + 1;
          const idx = Math.min(cur.schedule.reps, intervals.length - 1);
          const intervalDays = Math.max(1, Math.floor(intervals[idx] * 0.7));
          cur.schedule.stability = intervalDays;
          cur.schedule.state = 2;
          dueTime.setDate(dueTime.getDate() + intervalDays);

          if (wordId in reviewRelearnCounts && !reviewMasteredIds.has(wordId)) {{
            const failCount = (reviewRelearnCounts[wordId] || 0) + 1;
            reviewRelearnCounts[wordId] = failCount;
            cur.relearn_count = failCount;
            reviewQueue.push(cur);
          }} else {{
            reviewMasteredIds.add(wordId);
          }}
          reviewIndex++;
        }} else {{
          cur.schedule.reps = reps + 1;
          const idx = Math.min(cur.schedule.reps, intervals.length - 1);
          let intervalDays = intervals[idx];
          if (rating === 4) intervalDays = Math.ceil(intervalDays * 1.5);
          cur.schedule.stability = intervalDays;
          cur.schedule.state = 2;
          dueTime.setDate(dueTime.getDate() + intervalDays);

          reviewMasteredIds.add(wordId);
          reviewIndex++;
        }}
      }}

      cur.schedule.due = dueTime.toISOString();
      if (!cur.reviews) cur.reviews = [];
      cur.reviews.push({{ rating: rating, reviewed_at: new Date().toISOString() }});

      saveData();
      renderCurrentReviewCard();
    }}

    // ── 3. 随时自测模块 ──
    let quizWord = null;
    let quizAnswered = false;
    let flashKnowChosen = false;
    let quizType = 'choice';
    let quizChoices = [];
    let correctIdx = 0;

    function nextQuizQuestion() {{
      const filter = document.getElementById('quiz-list-filter').value;
      let pool = appData.words;
      if (filter !== 'all') {{
        const filterId = parseInt(filter);
        pool = pool.filter(w => w.list_id === filterId);
      }}

      if (pool.length < 2) {{
        alert('词库中单词过少（至少需要2个词），请先录入单词！');
        return;
      }}

      quizAnswered = false;
      flashKnowChosen = false;
      document.getElementById('quiz-feedback-box').style.display = 'none';
      const actBtn = document.getElementById('btn-quiz-actually-dont-know');
      const confBtn = document.getElementById('btn-quiz-confirm-know');
      if (actBtn) actBtn.style.display = 'none';
      if (confBtn) confBtn.style.display = 'none';

      quizWord = pool[Math.floor(Math.random() * pool.length)];

      const typeSelect = document.getElementById('quiz-type-select').value;
      if (typeSelect === 'random') {{
        const types = ['flash', 'choice', 'spell', 'blank'];
        quizType = types[Math.floor(Math.random() * types.length)];
      }} else {{
        quizType = typeSelect;
      }}

      if (quizType === 'choice' && pool.length < 4) {{
        quizType = 'flash';
      }}

      const listObj = appData.word_lists.find(l => l.id === quizWord.list_id) || {{ name: '默认词单', color: '#2563EB' }};
      const badge = document.getElementById('quiz-list-badge');
      badge.textContent = listObj.name;
      badge.style.backgroundColor = listObj.color;

      const choiceBox = document.getElementById('quiz-choices-container');
      const inputBox = document.getElementById('quiz-input-container');
      const flashBox = document.getElementById('quiz-flash-container');

      if (quizType === 'flash') {{
        document.getElementById('quiz-type-badge').textContent = '👀 看英文选认不认识 (按 1/2)';
        const phoneticHtml = quizWord.phonetic ? ` <span style="font-size: 1rem; color: #94A3B8; font-weight: normal;">${{esc(quizWord.phonetic)}}</span>` : '';
        document.getElementById('quiz-question-title').innerHTML = `
          <div style="display: flex; align-items: center; gap: 12px; margin-bottom: 6px;">
            <span style="font-size: 1.6rem; font-weight: 700; color: #F8FAFC;">${{esc(quizWord.word)}}</span>
            ${{phoneticHtml}}
            <button class="btn btn-outline" style="padding: 3px 8px; font-size: 0.8rem;" onclick="speakWord(quizWord.word)">🔊 (R)</button>
          </div>
          <div style="font-size: 0.92rem; color: var(--text-muted); font-weight: normal;">请在脑海中回忆中文释义，然后选择：</div>
        `;
        flashBox.style.display = 'flex';
        choiceBox.style.display = 'none';
        inputBox.style.display = 'none';
        document.getElementById('btn-flash-know').disabled = false;
        document.getElementById('btn-flash-dont-know').disabled = false;

      }} else if (quizType === 'choice') {{
        flashBox.style.display = 'none';
        document.getElementById('quiz-type-badge').textContent = '📝 释义选择题 (按 A/B/C/D)';
        document.getElementById('quiz-question-title').textContent = `下列哪个是「${{quizWord.word}}」的正确释义？`;
        choiceBox.style.display = 'flex';
        inputBox.style.display = 'none';

        // 生成干扰项
        const distractors = appData.words.filter(w => w.id !== quizWord.id);
        distractors.sort(() => 0.5 - Math.random());
        quizChoices = [quizWord.meaning, ...(distractors.slice(0, 3).map(d => d.meaning))];
        quizChoices.sort(() => 0.5 - Math.random());
        correctIdx = quizChoices.indexOf(quizWord.meaning);

        choiceBox.innerHTML = '';
        quizChoices.forEach((c, i) => {{
          const btn = document.createElement('button');
          btn.className = 'btn btn-outline';
          btn.style.justifyContent = 'flex-start';
          btn.style.textAlign = 'left';
          btn.style.padding = '12px 16px';
          // 使用 esc 防吞字
          btn.innerHTML = `<span style="font-weight: 700; width: 24px;">[${{String.fromCharCode(65+i)}}]</span> ${{esc(c)}}`;
          btn.onclick = () => onChoiceSelect(i);
          choiceBox.appendChild(btn);
        }});

      }} else if (quizType === 'spell') {{
        flashBox.style.display = 'none';
        document.getElementById('quiz-type-badge').textContent = '🔤 单词拼写题 (按 Enter 提交)';
        document.getElementById('quiz-question-title').textContent = `根据释义写出英文单词：\n${{quizWord.meaning}}`;
        choiceBox.style.display = 'none';
        inputBox.style.display = 'block';
        document.getElementById('quiz-answer-input').value = '';
        document.getElementById('quiz-answer-input').focus();

      }} else if (quizType === 'blank') {{
        flashBox.style.display = 'none';
        document.getElementById('quiz-type-badge').textContent = '📖 例句填空题 (按 Enter 提交)';
        let qText = quizWord.meaning;
        if (quizWord.example && quizWord.example.toLowerCase().includes(quizWord.word.toLowerCase())) {{
          const reg = new RegExp(quizWord.word, 'gi');
          qText = quizWord.example.replace(reg, '_______');
        }}
        document.getElementById('quiz-question-title').textContent = `填写空白处单词：\n${{qText}}`;
        choiceBox.style.display = 'none';
        inputBox.style.display = 'block';
        document.getElementById('quiz-answer-input').value = '';
        document.getElementById('quiz-answer-input').focus();
      }}
    }}

    function markQuizWordAgain(wordObj) {{
      if (!wordObj) return;
      if (!wordObj.schedule) wordObj.schedule = {{ reps: 0, lapses: 0, state: 0, stability: 0.1 }};
      const isProtected = isScheduleProtected(wordObj.schedule);
      wordObj.schedule.lapses = (wordObj.schedule.lapses || 0) + 1;
      if (isProtected) {{
        // 长期掌握词保护：不降级、不抹掉进度，仅降低稳定性并今日快速巩固
        wordObj.schedule.stability = Math.max(3.5, (wordObj.schedule.stability || 7.0) * 0.7);
        wordObj.schedule.state = 2;
        wordObj.schedule.reps = (wordObj.schedule.reps || 0) + 1;
      }} else {{
        wordObj.schedule.reps = 0;
        wordObj.schedule.state = 1;
        wordObj.schedule.stability = 0.1;
      }}
      wordObj.schedule.due = new Date(Date.now() - 1000).toISOString();
      if (!wordObj.reviews) wordObj.reviews = [];
      wordObj.reviews.push({{ rating: 1, reviewed_at: new Date().toISOString() }});
      saveData();
    }}

    function onFlashSelect(know) {{
      if (quizAnswered || !quizWord) return;
      quizAnswered = true;
      flashKnowChosen = know;

      const btnKnow = document.getElementById('btn-flash-know');
      const btnDontKnow = document.getElementById('btn-flash-dont-know');
      if (btnKnow) btnKnow.disabled = true;
      if (btnDontKnow) btnDontKnow.disabled = true;

      speakWord(quizWord.word);

      const fb = document.getElementById('quiz-feedback-box');
      const text = document.getElementById('quiz-feedback-text');
      const actBtn = document.getElementById('btn-quiz-actually-dont-know');
      const confBtn = document.getElementById('btn-quiz-confirm-know');
      fb.style.display = 'block';

      if (!know) {{
        // 用户选不认识：全自动加入今日复习
        markQuizWordAgain(quizWord);

        if (actBtn) actBtn.style.display = 'none';
        if (confBtn) confBtn.style.display = 'none';
        text.innerHTML = `
          <div style="color: #EF4444; font-size: 1.05rem; margin-bottom: 6px;">❌ 没记牢 · 已全自动加入今日复习队列！</div>
          <div style="font-size: 1rem; color: #F8FAFC; margin-bottom: 4px;"><b>${{esc(quizWord.word)}}</b> <span style="color: #94A3B8;">${{esc(quizWord.phonetic || '')}}</span></div>
          <div style="color: #CBD5E1; font-size: 0.95rem;">${{esc(quizWord.meaning)}}</div>
          ${{quizWord.example ? `<div style="color: #94A3B8; font-size: 0.85rem; margin-top: 4px; font-style: italic;">${{esc(quizWord.example)}}</div>` : ''}}
        `;
      }} else {{
        // 用户选认识：展示释义核对，并提供“其实不认识”与“确实记对了”
        if (actBtn) actBtn.style.display = 'inline-block';
        if (confBtn) confBtn.style.display = 'inline-block';
        text.innerHTML = `
          <div style="color: #10B981; font-size: 1.05rem; margin-bottom: 6px;">👀 释义已揭晓，请核对是否正确：</div>
          <div style="font-size: 1rem; color: #F8FAFC; margin-bottom: 4px;"><b>${{esc(quizWord.word)}}</b> <span style="color: #94A3B8;">${{esc(quizWord.phonetic || '')}}</span></div>
          <div style="color: #CBD5E1; font-size: 0.95rem;">${{esc(quizWord.meaning)}}</div>
          ${{quizWord.example ? `<div style="color: #94A3B8; font-size: 0.85rem; margin-top: 4px; font-style: italic;">${{esc(quizWord.example)}}</div>` : ''}}
        `;
      }}
    }}

    function onActuallyDontKnow() {{
      if (!quizWord) return;
      flashKnowChosen = false;

      // 自动纳入今日复习队列
      markQuizWordAgain(quizWord);

      const actBtn = document.getElementById('btn-quiz-actually-dont-know');
      const confBtn = document.getElementById('btn-quiz-confirm-know');
      if (actBtn) actBtn.style.display = 'none';
      if (confBtn) confBtn.style.display = 'none';

      const text = document.getElementById('quiz-feedback-text');
      text.innerHTML = `
        <div style="color: #EA580C; font-size: 1.05rem; margin-bottom: 6px;">⚠️ 已更正为「其实不认识」！该词已全自动纳入今日复习队列，稍后循环强化！</div>
        <div style="font-size: 1rem; color: #F8FAFC; margin-bottom: 4px;"><b>${{esc(quizWord.word)}}</b> <span style="color: #94A3B8;">${{esc(quizWord.phonetic || '')}}</span></div>
        <div style="color: #CBD5E1; font-size: 0.95rem;">${{esc(quizWord.meaning)}}</div>
        ${{quizWord.example ? `<div style="color: #94A3B8; font-size: 0.85rem; margin-top: 4px; font-style: italic;">${{esc(quizWord.example)}}</div>` : ''}}
      `;
      showToast('⚠️ 已更正为「其实不认识」，已放入今日复习！');
    }}

    function onChoiceSelect(idx) {{
      if (quizAnswered) return;
      quizAnswered = true;
      const buttons = document.querySelectorAll('#quiz-choices-container .btn');
      const isCorrect = idx === correctIdx;

      buttons.forEach((btn, i) => {{
        if (i === correctIdx) {{
          btn.style.backgroundColor = '#10B981';
          btn.style.borderColor = '#10B981';
        }} else if (i === idx && !isCorrect) {{
          btn.style.backgroundColor = '#EF4444';
          btn.style.borderColor = '#EF4444';
        }}
        btn.disabled = true;
      }});

      speakWord(quizWord.word);
      showQuizFeedback(isCorrect);
    }}

    function submitQuizInput() {{
      if (quizAnswered || !quizWord) return;
      quizAnswered = true;
      const userAns = document.getElementById('quiz-answer-input').value.trim().toLowerCase();
      const isCorrect = userAns === quizWord.word.toLowerCase();
      speakWord(quizWord.word);
      showQuizFeedback(isCorrect);
    }}

    function addQuizWordToReviewDirectly() {{
      if (!quizWord) return;
      markQuizWordAgain(quizWord);
      showToast(`📥 已将「${{quizWord.word}}」加入今日复习队列！`);
    }}

    function showQuizFeedback(isCorrect) {{
      const fb = document.getElementById('quiz-feedback-box');
      const text = document.getElementById('quiz-feedback-text');
      const actBtn = document.getElementById('btn-quiz-actually-dont-know');
      const confBtn = document.getElementById('btn-quiz-confirm-know');
      if (actBtn) actBtn.style.display = 'none';
      if (confBtn) confBtn.style.display = 'none';
      fb.style.display = 'block';

      if (isCorrect) {{
        text.innerHTML = `<span style="color: #10B981;">✅ 回答正确！</span> 正确单词是 <b>${{esc(quizWord.word)}}</b> ${{esc(quizWord.phonetic || '')}}`;
      }} else {{
        // 自动加入今日复习！
        markQuizWordAgain(quizWord);
        text.innerHTML = `<span style="color: #EF4444;">❌ 答错了 · 已自动加入今日复习！</span><br>正确答案是 <b>${{esc(quizWord.word)}}</b> ${{esc(quizWord.phonetic || '')}}<br><span style="font-size: 0.88rem; color: #94A3B8;">${{esc(quizWord.meaning)}}</span>`;
      }}
    }}

    function rateQuizCard(rating) {{
      if (!quizWord) return;
      if (!quizWord.schedule) quizWord.schedule = {{ reps: 0, lapses: 0, state: 0, stability: 0 }};
      const LEARNING_STEPS = [10, 60, 240];
      const intervals = [1, 2, 4, 7, 15, 30, 60];
      let dueTime = new Date();

      const state = quizWord.schedule.state || 0;
      const reps = quizWord.schedule.reps || 0;
      const stability = quizWord.schedule.stability || 0;
      const lapses = quizWord.schedule.lapses || 0;
      const inLearning = isScheduleInLearning(quizWord.schedule);
      const isProtected = isScheduleProtected(quizWord.schedule);

      if (rating === 1) {{
        if (isProtected) {{
          quizWord.schedule.lapses = lapses + 1;
          quizWord.schedule.reps = reps + 1;
          quizWord.schedule.stability = Math.max(3.5, stability * 0.7);
          quizWord.schedule.state = 2;
          dueTime = new Date(Date.now() + 60 * 60 * 1000); // 1小时后快速巩固
        }} else {{
          quizWord.schedule.lapses = lapses + 1;
          quizWord.schedule.reps = 0;
          quizWord.schedule.state = 1;
          quizWord.schedule.stability = 0.1;
          dueTime = new Date(Date.now() + LEARNING_STEPS[0] * 60 * 1000); // 10分钟后
        }}
      }} else if (inLearning) {{
        if (rating === 2) {{
          const stepIdx = Math.min(reps, LEARNING_STEPS.length - 1);
          dueTime = new Date(Date.now() + LEARNING_STEPS[stepIdx] * 60 * 1000);
          quizWord.schedule.state = 1;
        }} else {{
          const newReps = reps + 1;
          quizWord.schedule.reps = newReps;
          if (newReps >= LEARNING_STEPS.length) {{
            quizWord.schedule.state = 2;
            quizWord.schedule.stability = 1.0;
            dueTime = new Date(Date.now() + 24 * 60 * 60 * 1000);
          }} else {{
            quizWord.schedule.state = 1;
            const nextMins = LEARNING_STEPS[newReps];
            dueTime = new Date(Date.now() + nextMins * 60 * 1000);
          }}
        }}
      }} else {{
        quizWord.schedule.reps = reps + 1;
        const idx = Math.min(quizWord.schedule.reps, intervals.length - 1);
        let intervalDays = intervals[idx];
        if (rating === 2) intervalDays = Math.max(1, Math.floor(intervalDays * 0.7));
        if (rating === 4) intervalDays = Math.ceil(intervalDays * 1.5);
        quizWord.schedule.stability = intervalDays;
        quizWord.schedule.state = 2;
        dueTime.setDate(dueTime.getDate() + intervalDays);
      }}

      quizWord.schedule.due = dueTime.toISOString();
      if (!quizWord.reviews) quizWord.reviews = [];
      quizWord.reviews.push({{ rating: rating, reviewed_at: new Date().toISOString() }});
      saveData();
      nextQuizQuestion();
    }}

    // ── 4. 我的词库模块（全面 XSS 转义与防漏字） ──
    function renderWordListTable() {{
      const search = document.getElementById('words-search-input').value.trim().toLowerCase();
      const filter = document.getElementById('words-list-filter').value;
      const container = document.getElementById('word-cards-list');

      let list = appData.words;
      let isCrossListSearch = false;

      if (search) {{
        const matchFn = (w) => {{
          const wText = (w.word || '').toLowerCase();
          const wMean = (w.meaning || '').toLowerCase();
          const wNote = (w.note || '').toLowerCase();
          return wText.includes(search) || wMean.includes(search) || wNote.includes(search);
        }};

        if (filter !== 'all') {{
          const fid = parseInt(filter);
          const inList = list.filter(w => w.list_id === fid && matchFn(w));
          if (inList.length > 0) {{
            list = inList;
          }} else {{
            // 若当前词单未匹配到，自动跨词单全库搜索
            const globalMatches = list.filter(matchFn);
            if (globalMatches.length > 0) {{
              list = globalMatches;
              isCrossListSearch = true;
            }} else {{
              list = [];
            }}
          }}
        }} else {{
          list = list.filter(matchFn);
        }}
      }} else if (filter !== 'all') {{
        const fid = parseInt(filter);
        list = list.filter(w => w.list_id === fid);
      }}

      const renameBtn = document.getElementById('btn-rename-list');
      const deleteBtn = document.getElementById('btn-delete-list');
      if (filter === 'all' || isCrossListSearch) {{
        renameBtn.disabled = true;
        deleteBtn.disabled = true;
      }} else {{
        renameBtn.disabled = false;
        deleteBtn.disabled = false;
      }}

      if (isCrossListSearch) {{
        document.getElementById('words-count-info').textContent = `共 ${{list.length}} 个单词（当前词单无匹配，已在全词库中搜索到）`;
      }} else {{
        document.getElementById('words-count-info').textContent = `共 ${{list.length}} 个单词`;
      }}
      container.innerHTML = '';

      if (list.length === 0) {{
        container.innerHTML = '<div class="card" style="text-align: center; color: var(--text-muted); padding: 40px;">暂无匹配单词</div>';
        return;
      }}

      list.forEach(w => {{
        const item = document.createElement('div');
        item.className = 'card';
        item.style.padding = '12px 16px';
        item.style.display = 'flex';
        item.style.alignItems = 'center';
        item.style.justifyContent = 'space-between';
        item.style.gap = '12px';

        const listObj = appData.word_lists.find(l => l.id === w.list_id) || {{ name: '默认', color: '#2563EB' }};
        const stateVal = w.schedule ? w.schedule.state : 0;
        const stateTxt = STATE_LABELS[stateVal] || '新';
        const stateCol = STATE_COLORS[stateVal] || '#8B5CF6';

        item.innerHTML = `
          <div style="display: flex; align-items: center; gap: 10px; flex: 1; min-width: 0;">
            <span class="badge" style="background-color: ${{listObj.color}}; white-space: nowrap;">${{esc(listObj.name.substring(0, 10))}}</span>
            <span class="badge" style="background-color: ${{stateCol}};">${{stateTxt}}</span>
            <div style="flex: 1; min-width: 0;">
              <div style="display: flex; align-items: baseline; gap: 8px;">
                <span style="font-weight: 700; font-size: 1.05rem;">${{esc(w.word)}}</span>
                <span style="color: var(--accent-blue); font-size: 0.82rem; font-family: monospace;">${{esc(w.phonetic || '')}}</span>
              </div>
              <div style="color: var(--text-muted); font-size: 0.85rem; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;">
                ${{esc(w.meaning)}} ${{w.note ? ` | 📌 ${{esc(w.note)}}` : ''}}
              </div>
            </div>
          </div>
          <div style="display: flex; gap: 6px; align-items: center;">
            <button class="btn btn-outline" style="padding: 6px 8px;" onclick="speakWord('${{esc(w.word)}}')">🔊</button>
            <button class="btn btn-outline" style="padding: 6px 8px;" onclick="openEditWordModal(${{w.id}})">✏️</button>
            <button class="btn btn-outline" style="padding: 6px 8px; color: var(--danger);" onclick="deleteWordAction(${{w.id}})">🗑</button>
          </div>
        `;
        container.appendChild(item);
      }});
    }}

    function deleteWordAction(wordId) {{
      const wordObj = appData.words.find(w => w.id === wordId);
      if (!wordObj) return;
      if (confirm(`确定删除单词「${{wordObj.word}}」？`)) {{
        appData.words = appData.words.filter(w => w.id !== wordId);
        // 记入墓碑，避免下次更新网页时内置快照把这个词又合并回来
        rememberDeletedWord(wordObj.word);
        saveData();
        showToast('已删除单词');
        renderWordListTable();
      }}
    }}

    function openEditWordModal(wordId) {{
      const w = appData.words.find(w => w.id === wordId);
      if (!w) return;
      document.getElementById('edit-word-id').value = w.id;
      document.getElementById('edit-word-title').textContent = `✏️ 编辑单词：${{w.word}}`;
      document.getElementById('edit-word-list').value = w.list_id;
      document.getElementById('edit-word-phonetic').value = w.phonetic || '';
      document.getElementById('edit-word-meaning').value = w.meaning || '';
      document.getElementById('edit-word-example').value = w.example || '';
      document.getElementById('edit-word-note').value = w.note || '';

      document.getElementById('modal-edit-word').classList.add('active');
    }}

    function saveEditWordAction() {{
      const wid = parseInt(document.getElementById('edit-word-id').value);
      const w = appData.words.find(x => x.id === wid);
      if (!w) return;

      w.list_id = parseInt(document.getElementById('edit-word-list').value);
      w.phonetic = document.getElementById('edit-word-phonetic').value.trim();
      w.meaning = document.getElementById('edit-word-meaning').value.trim() || '（暂无释义）';
      w.example = document.getElementById('edit-word-example').value.trim();
      w.note = document.getElementById('edit-word-note').value.trim();

      saveData();
      closeModals();
      showToast('单词已保存');
      renderWordListTable();
    }}

    // ── 词单管理 ──
    let selectedColor = THEME_COLORS[0];
    function setupColorPicker() {{
      const box = document.getElementById('color-picker-group');
      box.innerHTML = '';
      THEME_COLORS.forEach(c => {{
        const btn = document.createElement('div');
        btn.style.width = '26px';
        btn.style.height = '26px';
        btn.style.borderRadius = '50%';
        btn.style.backgroundColor = c;
        btn.style.cursor = 'pointer';
        btn.style.border = c === selectedColor ? '2px solid white' : 'none';
        btn.onclick = () => {{
          selectedColor = c;
          setupColorPicker();
        }};
        box.appendChild(btn);
      }});
    }}

    function openNewListModal() {{
      document.getElementById('new-list-name').value = '';
      document.getElementById('new-list-desc').value = '';
      document.getElementById('modal-new-list').classList.add('active');
    }}

    function saveNewListAction() {{
      const name = document.getElementById('new-list-name').value.trim();
      if (!name) {{
        alert('请输入词单名称！');
        return;
      }}
      if (appData.word_lists.some(l => l.name === name)) {{
        alert('已存在同名词单！');
        return;
      }}

      const newId = generateUniqueId();
      appData.word_lists.push({{
        id: newId,
        name: name,
        description: document.getElementById('new-list-desc').value.trim(),
        color: selectedColor,
        created_at: new Date().toISOString()
      }});

      saveData();
      updateAllListDropdowns();
      closeModals();
      showToast(`已创建词单「${{name}}」`);
    }}

    function openRenameListModal() {{
      const currentListId = parseInt(document.getElementById('words-list-filter').value);
      const l = appData.word_lists.find(x => x.id === currentListId);
      if (!l) return;
      document.getElementById('rename-list-name').value = l.name;
      document.getElementById('rename-list-desc').value = l.description || '';
      document.getElementById('modal-rename-list').classList.add('active');
    }}

    function saveRenameListAction() {{
      const currentListId = parseInt(document.getElementById('words-list-filter').value);
      const l = appData.word_lists.find(x => x.id === currentListId);
      if (!l) return;

      const newName = document.getElementById('rename-list-name').value.trim();
      if (!newName) return;

      l.name = newName;
      l.description = document.getElementById('rename-list-desc').value.trim();
      saveData();
      updateAllListDropdowns();
      closeModals();
      showToast('词单已更新');
      renderWordListTable();
    }}

    function deleteCurrentListAction() {{
      const currentListId = parseInt(document.getElementById('words-list-filter').value);
      if (appData.word_lists.length <= 1) {{
        alert('至少保留一个词单，无法全部删除！');
        return;
      }}
      const l = appData.word_lists.find(x => x.id === currentListId);
      if (!l) return;

      const count = appData.words.filter(w => w.list_id === currentListId).length;
      if (confirm(`确定删除词单「${{l.name}}」？\n该词单内包含 ${{count}} 个单词，删除后单词将安全自动转移到默认词单。`)) {{
        const fallback = appData.word_lists.find(x => x.id !== currentListId);
        appData.words.forEach(w => {{
          if (w.list_id === currentListId) w.list_id = fallback.id;
        }});
        appData.word_lists = appData.word_lists.filter(x => x.id !== currentListId);
        saveData();
        updateAllListDropdowns();
        showToast(`已删除词单「${{l.name}}」`);
        renderWordListTable();
      }}
    }}

    function closeModals() {{
      document.querySelectorAll('.modal-overlay').forEach(el => el.classList.remove('active'));
    }}

    // ── 4. 📖 拉通复习模块 ──
    let sweepQueue = [];
    let sweepIndex = 0;
    let sweepRevealed = false;
    let sweepQueuedCount = 0;  // 答错并加入今日复习的词数
    let sweepKnownCount = 0;   // 记住了的词数
    let sweepTotal = 0;

    // 进入拉通复习 Tab 时只刷新下拉框，不自动开始
    function initSweepOnEnter() {{
      // 如果还没开始过（sweep-start-state 可见），就只刷新UI
      const startState = document.getElementById('sweep-start-state');
      if (startState && startState.style.display !== 'none') {{
        // 什么都不做，让用户手动点开始
        return;
      }}
      // 如果已经在复习中或已完成，维持原状
    }}

    function initSweepReview() {{
      const filter = document.getElementById('sweep-list-filter').value;
      const batchSize = parseInt(document.getElementById('sweep-batch-size').value) || 0;
      const order = document.getElementById('sweep-order').value;

      // 获取词池
      let pool = appData.words.slice();
      if (filter !== 'all') {{
        const filterId = parseInt(filter);
        pool = pool.filter(w => w.list_id === filterId);
      }}

      if (pool.length === 0) {{
        showToast('⚠️ 该词单没有单词！');
        return;
      }}

      // 排序
      if (order === 'random') {{
        // Fisher-Yates 随机洗牌
        for (let i = pool.length - 1; i > 0; i--) {{
          const j = Math.floor(Math.random() * (i + 1));
          [pool[i], pool[j]] = [pool[j], pool[i]];
        }}
      }}
      // 按批次截取（0 = 全部）
      if (batchSize > 0) {{
        pool = pool.slice(0, batchSize);
      }}

      sweepQueue = pool;
      sweepIndex = 0;
      sweepRevealed = false;
      sweepQueuedCount = 0;
      sweepKnownCount = 0;
      sweepTotal = pool.length;

      // 切换UI到复习卡片模式
      document.getElementById('sweep-start-state').style.display = 'none';
      document.getElementById('sweep-done-state').style.display = 'none';
      document.getElementById('sweep-card-container').style.display = 'flex';

      renderSweepCard();
    }}

    function renderSweepCard() {{
      const done = sweepIndex >= sweepQueue.length;
      const cardContainer = document.getElementById('sweep-card-container');
      const doneState = document.getElementById('sweep-done-state');
      const progressBar = document.getElementById('sweep-progress-bar');
      const progressCounter = document.getElementById('sweep-progress-counter');

      // 更新进度条
      const pct = sweepTotal > 0 ? Math.round((sweepIndex / sweepTotal) * 100) : 0;
      progressBar.style.width = pct + '%';
      progressCounter.textContent = sweepIndex + ' / ' + sweepTotal;

      if (done) {{
        // 完成：展示战报
        cardContainer.style.display = 'none';
        doneState.style.display = 'block';

        document.getElementById('sweep-stat-total').textContent = sweepTotal;
        document.getElementById('sweep-stat-known').textContent = sweepKnownCount;
        document.getElementById('sweep-stat-queued').textContent = sweepQueuedCount;

        const queuedTip = document.getElementById('sweep-goto-review-tip');
        const gotoBtn = document.getElementById('sweep-btn-goto-review');
        if (sweepQueuedCount > 0) {{
          document.getElementById('sweep-queued-count').textContent = sweepQueuedCount;
          queuedTip.style.display = 'block';
          gotoBtn.style.display = 'inline-block';
        }} else {{
          queuedTip.style.display = 'none';
          gotoBtn.style.display = 'none';
        }}

        const desc = sweepQueuedCount > 0
          ? `共复习 ${{sweepTotal}} 个词，${{sweepKnownCount}} 个记住，${{sweepQueuedCount}} 个已加入今日复习队列强化。`
          : `太棒了！共复习 ${{sweepTotal}} 个词，全部记住，无需额外强化！`;
        document.getElementById('sweep-done-desc').textContent = desc;
        return;
      }}

      // 渲染当前卡片
      const cur = sweepQueue[sweepIndex];
      const list = appData.word_lists.find(l => l.id === cur.list_id) || {{}};
      const listColor = list.color || '#2563EB';

      document.getElementById('sweep-card-list-badge').textContent = list.name || '--';
      document.getElementById('sweep-card-list-badge').style.backgroundColor = listColor;

      // 状态文字
      const sch = cur.schedule;
      let stateText = '新词';
      if (sch) {{
        if (sch.state === 2 && (sch.stability || 0) >= 7) stateText = `✅ 长期掌握 (稳定性: ${{Math.round(sch.stability)}}天)`;
        else if (sch.state === 2) stateText = `📗 复习中 (稳定性: ${{Math.round(sch.stability || 1)}}天)`;
        else if (sch.state === 1 || sch.state === 3) stateText = `🔄 学习中 (第 ${{sch.reps || 0}} 轮)`;
      }}
      document.getElementById('sweep-card-state-text').textContent = stateText;

      document.getElementById('sweep-card-word').textContent = cur.word || '';
      document.getElementById('sweep-card-phonetic').textContent = cur.phonetic || '';

      // 重置翻转状态
      sweepRevealed = false;
      document.getElementById('sweep-cover').style.display = 'block';
      document.getElementById('sweep-answer').style.display = 'none';
      document.getElementById('sweep-btn-reveal').style.display = 'block';
      document.getElementById('sweep-rating-buttons').style.display = 'none';
    }}

    function revealSweepCard() {{
      if (sweepRevealed || sweepIndex >= sweepQueue.length) return;
      sweepRevealed = true;
      const cur = sweepQueue[sweepIndex];

      document.getElementById('sweep-cover').style.display = 'none';
      document.getElementById('sweep-answer').style.display = 'block';
      document.getElementById('sweep-btn-reveal').style.display = 'none';
      document.getElementById('sweep-rating-buttons').style.display = 'grid';

      document.getElementById('sweep-card-meaning').textContent = cur.meaning || '';
      document.getElementById('sweep-card-example').textContent = cur.example || '';
      document.getElementById('sweep-card-note').textContent = cur.note ? ('📝 ' + cur.note) : '';

      speakWord(cur.word);
    }}

    function rateSweepCard(rating) {{
      if (!sweepRevealed && rating !== 4) {{
        // 允许直接按4跳过（太简单，不翻牌）；其他先要求翻牌
        revealSweepCard();
        return;
      }}
      if (sweepIndex >= sweepQueue.length) return;

      const cur = sweepQueue[sweepIndex];

      if (rating === 1 || rating === 2) {{
        // 答错或模糊：加入今日复习（due = now，下次切到今日复习就会出现）
        markQuizWordAgain(cur);
        sweepQueuedCount++;
      }} else {{
        // rating 3 或 4：记住了或太简单，不额外干预 FSRS 计划
        sweepKnownCount++;
      }}

      sweepIndex++;
      sweepRevealed = false;
      renderSweepCard();
    }}

    function sweepSpeakCurrent() {{
      if (sweepIndex < sweepQueue.length) {{
        speakWord(sweepQueue[sweepIndex].word);
      }}
    }}

    // ── 5. 学习统计模块与 Canvas 饼图绘制 ──
    function renderStatsView() {{
      const total = appData.words.length;
      const endOfTodayIso = getEndOfTodayIso();
      const dueCount = appData.words.filter(w => !w.photo_pending && (!w.schedule || !w.schedule.due || w.schedule.due <= endOfTodayIso)).length;
      const masteredCount = appData.words.filter(w => w.schedule && w.schedule.state === 2 && w.schedule.stability > 21).length;
      const newCount = appData.words.filter(w => !w.schedule || !w.schedule.reps || w.schedule.reps === 0).length;

      document.getElementById('stat-total-words').textContent = total;
      document.getElementById('stat-total-lists-sub').textContent = `共 ${{appData.word_lists.length}} 个独立词单`;
      document.getElementById('stat-due-today').textContent = dueCount;
      document.getElementById('stat-mastered-words').textContent = masteredCount;
      document.getElementById('stat-new-words').textContent = newCount;

      const breakdownBox = document.getElementById('stats-lists-breakdown');
      breakdownBox.innerHTML = '';

      appData.word_lists.forEach(l => {{
        const wordsInList = appData.words.filter(w => w.list_id === l.id);
        const listTotal = wordsInList.length;
        const listDue = wordsInList.filter(w => !w.photo_pending && (!w.schedule || !w.schedule.due || w.schedule.due <= endOfTodayIso)).length;
        const listMastered = wordsInList.filter(w => w.schedule && w.schedule.state === 2 && w.schedule.stability > 21).length;
        const pct = listTotal > 0 ? Math.round((listMastered / listTotal) * 100) : 0;

        const row = document.createElement('div');
        row.style.display = 'flex';
        row.style.flexDirection = 'column';
        row.style.gap = '4px';
        row.innerHTML = `
          <div style="display: flex; justify-content: space-between; font-size: 0.85rem;">
            <span><span class="badge" style="background-color: ${{l.color}};">${{esc(l.name)}}</span> (总数: ${{listTotal}} | 待复习: ${{listDue}})</span>
            <span style="color: var(--text-muted);">已掌握: ${{listMastered}} (${{pct}}%)</span>
          </div>
          <div style="width: 100%; height: 6px; background-color: #334155; border-radius: 3px; overflow: hidden;">
            <div style="width: ${{pct}}%; height: 100%; background-color: ${{l.color}};"></div>
          </div>
        `;
        breakdownBox.appendChild(row);
      }});

      drawPieChart('chart-lists-pie', appData.word_lists.map(l => ({{
        label: l.name.replace(/[📘🎯📖🔥🎧]/g, '').trim(),
        value: appData.words.filter(w => w.list_id === l.id).length,
        color: l.color
      }})));

      const learningCount = Math.max(0, total - newCount - masteredCount);
      drawPieChart('chart-state-pie', [
        {{ label: "新词", value: newCount, color: "#8B5CF6" }},
        {{ label: "学习中", value: learningCount, color: "#F59E0B" }},
        {{ label: "已掌握", value: masteredCount, color: "#10B981" }}
      ]);
    }}

    function drawPieChart(canvasId, items) {{
      const canvas = document.getElementById(canvasId);
      if (!canvas) return;
      const ctx = canvas.getContext('2d');
      const w = canvas.width;
      const h = canvas.height;
      ctx.clearRect(0, 0, w, h);

      const validItems = items.filter(it => it.value > 0);
      const total = validItems.reduce((sum, it) => sum + it.value, 0);

      const cx = w / 2;
      const cy = h / 2;
      const radius = Math.min(cx, cy) - 20;

      if (total === 0) {{
        ctx.fillStyle = '#64748B';
        ctx.font = '13px sans-serif';
        ctx.textAlign = 'center';
        ctx.textBaseline = 'middle';
        ctx.fillText('暂无数据', cx, cy);
        return;
      }}

      let startAngle = -Math.PI / 2;
      validItems.forEach(it => {{
        const sliceAngle = (it.value / total) * 2 * Math.PI;
        ctx.beginPath();
        ctx.moveTo(cx, cy);
        ctx.arc(cx, cy, radius, startAngle, startAngle + sliceAngle);
        ctx.closePath();
        ctx.fillStyle = it.color;
        ctx.fill();

        const midAngle = startAngle + sliceAngle / 2;
        const textX = cx + Math.cos(midAngle) * (radius * 0.65);
        const textY = cy + Math.sin(midAngle) * (radius * 0.65);
        ctx.fillStyle = '#FFFFFF';
        ctx.font = 'bold 11px sans-serif';
        ctx.textAlign = 'center';
        ctx.textBaseline = 'middle';
        const pct = Math.round((it.value / total) * 100);
        if (pct >= 8) {{
          ctx.fillText(`${{it.label}} ${{pct}}%`, textX, textY);
        }}

        startAngle += sliceAngle;
      }});
    }}

    // ── ☁️ 云端同步与多端网络互通驱动 ──
    let cloudMode = 'server';

    function switchCloudMode(mode) {{
      cloudMode = mode;
      const btnServer = document.getElementById('tab-btn-cloud-server');
      const btnGist = document.getElementById('tab-btn-cloud-gist');
      const paneServer = document.getElementById('cloud-server-pane');
      const paneGist = document.getElementById('cloud-gist-pane');
      if (mode === 'server') {{
        if (btnServer) btnServer.className = 'btn btn-primary';
        if (btnGist) btnGist.className = 'btn btn-secondary';
        if (paneServer) paneServer.style.display = 'block';
        if (paneGist) paneGist.style.display = 'none';
      }} else {{
        if (btnServer) btnServer.className = 'btn btn-secondary';
        if (btnGist) btnGist.className = 'btn btn-primary';
        if (paneServer) paneServer.style.display = 'none';
        if (paneGist) paneGist.style.display = 'block';
      }}
      saveCloudSettings();
    }}

    function getCloudSettings() {{
      let cfg = {{
        mode: 'server',
        serverUrl: '',
        token: '',
        gistToken: '',
        gistId: '',
        autoLoad: false,
        autoSave: false,
        lastSync: ''
      }};
      try {{
        const raw = localStorage.getItem('toefl_cloud_sync_cfg');
        if (raw) Object.assign(cfg, JSON.parse(raw));
      }} catch(e) {{}}
      if (!cfg.serverUrl) {{
        if (window.location.protocol.startsWith('http')) {{
          cfg.serverUrl = window.location.origin;
        }} else {{
          cfg.serverUrl = 'http://localhost:8765';
        }}
      }}
      return cfg;
    }}

    function setServerUrlFast(url) {{
      const input = document.getElementById('cloud-server-url');
      if (input) {{
        input.value = url;
        saveCloudSettings();
        showToast('已填入地址：' + url);
        probeLocalServerStatus();
      }}
    }}

    async function probeLocalServerStatus() {{
      const cfg = getCloudSettings();
      if (cfg.mode !== 'server') return false;
      const serverUrl = (cfg.serverUrl || 'http://localhost:8765').replace(new RegExp('/+$'), '');
      try {{
        const headers = {{}};
        if (cfg.token) headers['X-Sync-Token'] = cfg.token;
        const controller = new AbortController();
        const tid = setTimeout(() => controller.abort(), 2000);
        const resp = await fetch(`${{serverUrl}}/api/status`, {{ method: 'GET', headers, signal: controller.signal }});
        clearTimeout(tid);
        if (resp.ok) {{
          const data = await resp.json();
          updateCloudStatusBadge(true, cfg.lastSync || '在线就绪');
          const fb = document.getElementById('cloud-feedback-msg');
          if (fb && !fb.textContent) {{
            fb.innerHTML = `<span style="color: #4ADE80;">🟢 已连接本机 Python 客户端 (${{serverUrl}}) · 词库共 ${{data.stats?.total_words || 0}} 词</span>`;
          }}
          return true;
        }}
      }} catch(e) {{
        // 未启动或离线
      }}
      return false;
    }}

    function initCloudSettingsUI() {{
      const cfg = getCloudSettings();
      const inputUrl = document.getElementById('cloud-server-url');
      const inputToken = document.getElementById('cloud-server-token');
      const inputGistToken = document.getElementById('cloud-gist-token');
      const inputGistId = document.getElementById('cloud-gist-id');
      const chkLoad = document.getElementById('cloud-auto-load');
      const chkSave = document.getElementById('cloud-auto-save');

      if (inputUrl) inputUrl.value = cfg.serverUrl || '';
      if (inputToken) inputToken.value = cfg.token || '';
      if (inputGistToken) inputGistToken.value = cfg.gistToken || '';
      if (inputGistId) inputGistId.value = cfg.gistId || '';
      if (chkLoad) chkLoad.checked = !!cfg.autoLoad;
      if (chkSave) chkSave.checked = !!cfg.autoSave;
      if (cfg.mode) switchCloudMode(cfg.mode);

      if (cfg.lastSync) {{
        updateCloudStatusBadge(true, cfg.lastSync);
      }}

      // 异步探测本机服务是否在线
      setTimeout(probeLocalServerStatus, 500);
    }}

    function saveCloudSettings() {{
      const cfg = {{
        mode: cloudMode,
        serverUrl: (document.getElementById('cloud-server-url')?.value || '').trim(),
        token: (document.getElementById('cloud-server-token')?.value || '').trim(),
        gistToken: (document.getElementById('cloud-gist-token')?.value || '').trim(),
        gistId: (document.getElementById('cloud-gist-id')?.value || '').trim(),
        autoLoad: !!document.getElementById('cloud-auto-load')?.checked,
        autoSave: !!document.getElementById('cloud-auto-save')?.checked,
      }};
      const old = getCloudSettings();
      cfg.lastSync = old.lastSync || '';
      try {{
        localStorage.setItem('toefl_cloud_sync_cfg', JSON.stringify(cfg));
      }} catch(e) {{}}
    }}

    function updateCloudStatusBadge(online, timeStr) {{
      const badge = document.getElementById('web-cloud-status-badge');
      if (!badge) return;
      if (online) {{
        badge.style.background = 'rgba(74, 222, 128, 0.2)';
        badge.style.color = '#4ADE80';
        badge.textContent = `🟢 已同步 (${{timeStr || new Date().toLocaleTimeString()}})`;
      }} else {{
        badge.style.background = 'rgba(248, 113, 113, 0.2)';
        badge.style.color = '#F87171';
        badge.textContent = '🔴 未连接云端';
      }}
    }}

    function triggerAutoCloudPush() {{
      const ccfg = getCloudSettings();
      if (ccfg.autoSave) {{
        if (ccfg.mode === 'gist' && ccfg.gistToken) {{
          triggerWebGistSync('push', true);
        }} else if (ccfg.serverUrl) {{
          triggerWebCloudSync('push', true);
        }}
      }}
    }}

    async function applySyncImportPayload(incoming, mode = 'merge') {{
      if (!incoming || (!Array.isArray(incoming.words) && !Array.isArray(incoming.word_lists))) {{
        throw new Error('无效的同步数据包');
      }}

      if (mode === 'overwrite') {{
        appData.word_lists = incoming.word_lists || [{{ id: 1, name: '默认词单', description: '', color: '#2563EB' }}];
        appData.words = incoming.words || [];
        appData.photo_state = incoming.photo_state || {{}};
        PhotoCore.ensure(appData);
        PhotoCore.reconcile(appData);
        clearDeletedWordKeys();
        dedupeWords(appData);
        await saveData();
        updateAllListDropdowns();
        renderAllViews();
        return {{ words_added: appData.words.length, schedules_updated: appData.words.length }};
      }}

      // 智能双向合并 (merge)：与「内置快照 / 云端拉取 / 文件导入」共用同一套内核
      const stats = mergeSyncPayloadInto(appData, incoming);

      // 合并后顺手清理重复单词，保证多端同步后词库依旧唯一
      const dedupeStats = dedupeWords(appData);
      stats.duplicates_removed = dedupeStats.duplicates_removed;

      await saveData();
      updateAllListDropdowns();
      renderAllViews();
      renderBuiltinSnapshotInfo();
      return stats;
    }}

    async function triggerWebCloudSync(direction = 'two_way', silent = false) {{
      saveCloudSettings();
      const cfg = getCloudSettings();
      let serverUrl = cfg.serverUrl.trim();
      if (!serverUrl) {{
        if (!silent) showToast('⚠️ 请先输入云端服务器地址');
        return;
      }}
      serverUrl = serverUrl.replace(new RegExp('/+$'), '');
      if (!serverUrl.startsWith('http://') && !serverUrl.startsWith('https://')) {{
        serverUrl = 'http://' + serverUrl;
      }}

      const fb = document.getElementById('cloud-feedback-msg');
      if (fb && !silent) fb.textContent = '⏳ 正在连接云端并同步...';

      const headers = {{ 'Content-Type': 'application/json' }};
      if (cfg.token) {{
        headers['X-Sync-Token'] = cfg.token;
        headers['Authorization'] = `Bearer ${{cfg.token}}`;
      }}

      try {{
        let pullStats = {{ words_added: 0, schedules_updated: 0 }};

        if (PhotoCore.ensure(appData).batches.length) {{
          const capabilityResp = await fetch(`${{serverUrl}}/api/status`, {{ headers }});
          const capability = capabilityResp.ok ? await capabilityResp.json() : {{}};
          if ((capability.sync_version || 0) < 3) throw new Error('服务器版本过旧，不能保存照片批次；请先升级服务器，并导出JSON备份');
        }}

        // 1. 拉取并合并 (pull 或 two_way)
        if (direction === 'pull' || direction === 'two_way') {{
          const resp = await fetch(`${{serverUrl}}/api/sync`, {{ method: 'GET', headers }});
          if (!resp.ok) {{
            throw new Error(`云端返回 HTTP ${{resp.status}}`);
          }}
          const remoteData = await resp.json();
          pullStats = await applySyncImportPayload(remoteData, 'merge');
        }}

        // 2. 推送合并后的全量数据回云端 (push 或 two_way)
        if (direction === 'push' || direction === 'two_way') {{
          const fullPayload = {{
            format: "toefl_vocab_sync",
            version: 3,
            photo_state: PhotoCore.ensure(appData),
            exported_at: new Date().toISOString(),
            summary: {{
              total_lists: appData.word_lists.length,
              total_words: appData.words.length
            }},
            word_lists: appData.word_lists,
            words: appData.words
          }};
          const pushResp = await fetch(`${{serverUrl}}/api/sync`, {{
            method: 'POST',
            headers,
            body: JSON.stringify(fullPayload)
          }});
          if (!pushResp.ok) {{
            throw new Error(`推送云端返回 HTTP ${{pushResp.status}}`);
          }}
        }}

        const nowStr = new Date().toLocaleTimeString();
        updateCloudStatusBadge(true, nowStr);
        cfg.lastSync = nowStr;
        localStorage.setItem('toefl_cloud_sync_cfg', JSON.stringify(cfg));

        const msg = direction === 'two_way' 
          ? `🎉 双向云同步成功！本地新增 ${{pullStats.words_added}} 词，更新 ${{pullStats.schedules_updated}} 进度`
          : (direction === 'pull' ? `📥 从云端拉取完成 (+${{pullStats.words_added}} 词)` : `📤 已成功推送全量数据至云端！`);

        if (fb) fb.textContent = `✅ ${{nowStr}} ${{msg}}`;
        if (!silent) showToast(msg);
      }} catch (err) {{
        updateCloudStatusBadge(false);
        let errMsg = `同步失败: ${{err.message}}`;
        if (serverUrl.includes('localhost') || serverUrl.includes('127.0.0.1') || serverUrl.includes('8765')) {{
          errMsg = `同步失败: ${{err.message}}。请检查服务器地址及版本。`;
        }}
        if (fb) fb.innerHTML = `<span style="color: #F87171;">❌ ${{errMsg}}</span>`;
        if (!silent) showToast(`❌ ${{errMsg}}`);
      }}
    }}

    async function triggerWebGistSync(direction = 'two_way', silent = false) {{
      saveCloudSettings();
      const cfg = getCloudSettings();
      const token = cfg.gistToken.trim();
      if (!token) {{
        if (!silent) showToast('⚠️ 请输入 GitHub Token');
        return;
      }}
      let gistId = cfg.gistId.trim();
      const fb = document.getElementById('cloud-feedback-msg');
      if (fb && !silent) fb.textContent = '⏳ 正在连接 GitHub Gist 同步...';

      const headers = {{
        'Accept': 'application/vnd.github.v3+json',
        'Authorization': `token ${{token}}`,
        'Content-Type': 'application/json'
      }};

      try {{
        if (!gistId) {{
          const listResp = await fetch('https://api.github.com/gists', {{ headers }});
          if (listResp.ok) {{
            const gists = await listResp.json();
            const found = gists.find(g => g.files && g.files['toefl_vocab_sync.json']);
            if (found) gistId = found.id;
          }}
        }}

        let pullStats = {{ words_added: 0, schedules_updated: 0 }};
        if (gistId && (direction === 'pull' || direction === 'two_way')) {{
          const gResp = await fetch(`https://api.github.com/gists/${{gistId}}`, {{ headers }});
          if (gResp.ok) {{
            const gDetail = await gResp.json();
            const f = gDetail.files && gDetail.files['toefl_vocab_sync.json'];
            if (f && f.content) {{
              const remoteData = JSON.parse(f.content);
              pullStats = await applySyncImportPayload(remoteData, 'merge');
            }}
          }}
        }}

        if (direction === 'push' || direction === 'two_way') {{
          const fullPayload = {{
            format: "toefl_vocab_sync",
            version: 3,
            photo_state: PhotoCore.ensure(appData),
            exported_at: new Date().toISOString(),
            word_lists: appData.word_lists,
            words: appData.words
          }};
          const contentStr = JSON.stringify(fullPayload, null, 2);
          if (gistId) {{
            await fetch(`https://api.github.com/gists/${{gistId}}`, {{
              method: 'PATCH',
              headers,
              body: JSON.stringify({{
                description: `TOEFL Vocab Sync Backup (${{new Date().toLocaleString()}})`,
                files: {{ 'toefl_vocab_sync.json': {{ content: contentStr }} }}
              }})
            }});
          }} else {{
            const cResp = await fetch('https://api.github.com/gists', {{
              method: 'POST',
              headers,
              body: JSON.stringify({{
                description: 'TOEFL Vocab Sync Backup - 托福词汇背诵助手云端同步库',
                public: false,
                files: {{ 'toefl_vocab_sync.json': {{ content: contentStr }} }}
              }})
            }});
            if (cResp.ok) {{
              const created = await cResp.json();
              gistId = created.id;
            }}
          }}
        }}

        if (gistId) {{
          cfg.gistId = gistId;
          const inputId = document.getElementById('cloud-gist-id');
          if (inputId) inputId.value = gistId;
        }}

        const nowStr = new Date().toLocaleTimeString();
        updateCloudStatusBadge(true, nowStr);
        cfg.lastSync = nowStr;
        localStorage.setItem('toefl_cloud_sync_cfg', JSON.stringify(cfg));

        const msg = `🎉 GitHub Gist 云端同步成功！(+${{pullStats.words_added}} 词)`;
        if (fb) fb.textContent = `✅ ${{nowStr}} ${{msg}}`;
        if (!silent) showToast(msg);
      }} catch (err) {{
        updateCloudStatusBadge(false);
        const errMsg = `Gist 同步失败: ${{err.message}}`;
        if (fb) fb.textContent = `❌ ${{errMsg}}`;
        if (!silent) showToast(`❌ ${{errMsg}}`);
      }}
    }}

    // ── 6. 数据同步模块（加固容灾与兼容性） ──
    function exportSyncFileAction() {{
      const payload = {{
        format: "toefl_vocab_sync",
        version: 3,
            photo_state: PhotoCore.ensure(appData),
        exported_at: new Date().toISOString(),
        summary: {{
          total_lists: appData.word_lists.length,
          total_words: appData.words.length
        }},
        word_lists: appData.word_lists,
        words: appData.words
      }};

      const blob = new Blob([JSON.stringify(payload, null, 2)], {{ type: 'application/json' }});
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      const timeStr = new Date().toISOString().slice(0, 10).replace(/-/g, '');
      a.href = url;
      a.download = `toefl_vocab_sync_${{timeStr}}.json`;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      setTimeout(() => URL.revokeObjectURL(url), 1000);
      showToast('✅ 已安全下载同步文件！');
    }}

    function copySyncDataAction() {{
      const payload = {{
        format: "toefl_vocab_sync",
        version: 3,
            photo_state: PhotoCore.ensure(appData),
        exported_at: new Date().toISOString(),
        word_lists: appData.word_lists,
        words: appData.words
      }};
      const text = JSON.stringify(payload);
      if (navigator.clipboard && navigator.clipboard.writeText) {{
        navigator.clipboard.writeText(text).then(() => {{
          showToast('📋 已将完整同步数据复制到剪贴板！');
        }}).catch(() => promptFallbackCopy(text));
      }} else {{
        promptFallbackCopy(text);
      }}
    }}

    function promptFallbackCopy(text) {{
      const ta = document.createElement('textarea');
      ta.value = text;
      document.body.appendChild(ta);
      ta.select();
      document.execCommand('copy');
      document.body.removeChild(ta);
      showToast('📋 已复制同步数据！');
    }}

    function handleFileImport(e) {{
      const file = e.target.files[0];
      if (!file) return;
      const reader = new FileReader();
      reader.onload = (event) => {{
        try {{
          const json = JSON.parse(event.target.result);
          applyImportData(json);
        }} catch(err) {{
          alert('同步文件解析失败：' + err);
        }}
      }};
      reader.readAsText(file);
      e.target.value = '';
    }}

    function importFromClipboardAction() {{
      if (navigator.clipboard && navigator.clipboard.readText) {{
        navigator.clipboard.readText().then(text => {{
          try {{
            applyImportData(JSON.parse(text));
          }} catch(err) {{
            manualPromptImport();
          }}
        }}).catch(() => manualPromptImport());
      }} else {{
        manualPromptImport();
      }}
    }}

    function manualPromptImport() {{
      const text = prompt('请粘贴同步 JSON 文本：');
      if (text) {{
        try {{
          applyImportData(JSON.parse(text));
        }} catch(e) {{
          alert('数据格式有误，解析失败：' + e);
        }}
      }}
    }}

    async function applyImportData(json) {{
      const mode = document.querySelector('input[name="sync-mode"]:checked').value;
      if (mode === 'overwrite') {{
        if (!confirm('确定完全覆盖当前词库？当前所有背诵进度将被替换！')) return;
      }}
      try {{
        const res = await applySyncImportPayload(json, mode);
        let msg = `✅ 成功同步 ${{res.words_added}} 个新词，更新 ${{res.schedules_updated}} 个进度！`;
        if (res.duplicates_removed) msg += ` 🧹 同时清理 ${{res.duplicates_removed}} 条重复单词`;
        showToast(msg, 4000);
      }} catch(e) {{
        alert('导入失败：' + e.message);
      }}
    }}

    function resetToInitialDataAction() {{
      if (confirm('确定将词库完全重置为初始状态？现有的自建词单和背诵打卡记录将被清除！建议先导出备份。')) {{
        appData = JSON.parse(JSON.stringify(INITIAL_DEFAULT_DATA));
        clearDeletedWordKeys();
        saveData();
        updateAllListDropdowns();
        showToast('已重置为初始词库！');
        location.reload();
      }}
    }}

    // ── 键盘快捷键统一调度 ──
    function setupKeyboardShortcuts() {{
      window.addEventListener('keydown', (e) => {{
        const activeTag = document.activeElement ? document.activeElement.tagName.toLowerCase() : '';
        if (activeTag === 'input' || activeTag === 'textarea' || activeTag === 'select') {{
          if (e.key === 'Enter' && document.activeElement.id === 'add-word-input') {{
            e.preventDefault();
            const meaning = document.getElementById('add-meaning-input').value.trim();
            if (!meaning) {{
              lookupWordAction();
            }} else {{
              saveNewWordAction();
            }}
          }}
          return;
        }}

        const key = e.key.toLowerCase();
        const activePage = document.querySelector('.view-page.active');
        if (!activePage) return;
        const pageId = activePage.id;

        if (pageId === 'view-review') {{
          if (key === ' ' || key === 'enter') {{
            e.preventDefault();
            if (!reviewRevealed) {{
              revealReviewCard();
            }} else {{
              rateReviewCard(3); // 默认 Good
            }}
          }} else if (key === '1') {{
            e.preventDefault();
            rateReviewCard(1);
          }} else if (key === '2') {{
            e.preventDefault();
            rateReviewCard(2);
          }} else if (key === '3') {{
            e.preventDefault();
            rateReviewCard(3);
          }} else if (key === '4') {{
            e.preventDefault();
            rateReviewCard(4);
          }} else if (key === 'r') {{
            e.preventDefault();
            speakCurrentWord();
          }}
        }} else if (pageId === 'view-sweep') {{
          if (key === ' ' || key === 'enter') {{
            e.preventDefault();
            if (!sweepRevealed) {{
              revealSweepCard();
            }} else {{
              rateSweepCard(3); // 默认 Good
            }}
          }} else if (key === '1') {{
            e.preventDefault();
            rateSweepCard(1);
          }} else if (key === '2') {{
            e.preventDefault();
            rateSweepCard(2);
          }} else if (key === '3') {{
            e.preventDefault();
            rateSweepCard(3);
          }} else if (key === '4') {{
            e.preventDefault();
            rateSweepCard(4);
          }} else if (key === 'r') {{
            e.preventDefault();
            sweepSpeakCurrent();
          }}
        }} else if (pageId === 'view-quiz') {{
          if (key === 'r' && quizWord) {{
            e.preventDefault();
            speakWord(quizWord.word);
            return;
          }}
          if (quizType === 'flash') {{
            if (!quizAnswered) {{
              if (key === '1' || key === 'y') {{
                e.preventDefault();
                onFlashSelect(true);
              }} else if (key === '2' || key === 'n') {{
                e.preventDefault();
                onFlashSelect(false);
              }}
            }} else if (flashKnowChosen) {{
              if (key === '0' || key === 'x') {{
                e.preventDefault();
                onActuallyDontKnow();
              }} else if (key === ' ' || key === 'enter' || key === '3') {{
                e.preventDefault();
                rateQuizCard(3);
              }} else if (key === '1') {{
                e.preventDefault();
                rateQuizCard(1);
              }} else if (key === '2') {{
                e.preventDefault();
                rateQuizCard(2);
              }} else if (key === '4') {{
                e.preventDefault();
                rateQuizCard(4);
              }}
            }} else {{
              if (key === ' ' || key === 'enter' || key === '1' || key === '3') {{
                e.preventDefault();
                nextQuizQuestion();
              }}
            }}
          }} else if (quizType === 'choice' && !quizAnswered) {{
            const map = {{ 'a': 0, '1': 0, 'b': 1, '2': 1, 'c': 2, '3': 2, 'd': 3, '4': 3 }};
            if (map[key] !== undefined && map[key] < quizChoices.length) {{
              e.preventDefault();
              onChoiceSelect(map[key]);
            }}
          }} else if (quizAnswered) {{
            if (key === ' ' || key === 'enter' || key === '3') {{
              e.preventDefault();
              rateQuizCard(3);
            }} else if (key === '1') {{
              e.preventDefault();
              rateQuizCard(1);
            }} else if (key === '2') {{
              e.preventDefault();
              rateQuizCard(2);
            }} else if (key === '4') {{
              e.preventDefault();
              rateQuizCard(4);
            }}
          }}
        }}
      }});
    }}

    window.addEventListener('DOMContentLoaded', initApp);
  </script>
</body>
</html>
'''

if BUILD_ARGS.cloud:
    core_path = BASE_DIR / 'cloud-site/lib/cloud-core.mjs'
    if not core_path.exists():
        core_path = BASE_DIR.parent / 'lib/cloud-core.mjs'
    core = core_path.read_text().replace("import './photo_core.js';", '').replace('export ', '')
    cloud_script = "const CloudReducer=(()=>{\n" + core + "\nreturn {applyOperations,emptyState,publicState};})();\n" + (BASE_DIR/'web/cloud_sync.js').read_text()
    html_content = html_content.replace("window.addEventListener('DOMContentLoaded', initApp);", cloud_script + "\nwindow.addEventListener('DOMContentLoaded', initApp);")
    for token in ['TOEFL_VOCAB_WEB_V2','TOEFL_VOCAB_BACKUP_V2','TOEFL_VOCAB_BUILTIN_MERGED','TOEFL_VOCAB_DELETED_WORDS','ToeflVocabStorage','TOEFL_PHOTO_VIEW']:
        html_content = html_content.replace("'"+token+"'", "'"+token+"_CLOUD_'+window.CLOUD_ACCOUNT.userId")
    html_content = html_content.replace('reviews.push({ rating:', 'reviews.push({ id: crypto.randomUUID(), rating:')
    html_content = html_content.replace('<title>托福词汇背诵助手 (网页版)</title>', '<title>托福词汇背诵助手 · 私有云端</title>')
    html_content = html_content.replace('<head>', '<head>\n<link rel="icon" href="/favicon.svg">')
    output = Path(BUILD_ARGS.output) if BUILD_ARGS.output else BASE_DIR/'cloud-site/lib/vocab.html'
else:
    output = Path(BUILD_ARGS.output) if BUILD_ARGS.output else BASE_DIR/'index.html'
output.parent.mkdir(parents=True, exist_ok=True)
output.write_text(html_content, encoding='utf-8')

print("Hardened index.html generated successfully!")
