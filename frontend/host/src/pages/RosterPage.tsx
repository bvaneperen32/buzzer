import { useEffect, useRef, useState } from 'react';
import { useParams, useNavigate } from 'react-router-dom';
import { ArrowLeft, Upload, X, Pencil } from 'lucide-react';
import { api } from '../lib/api';
import { Button } from '../components/ui/button';
import { Card, CardContent, CardHeader } from '../components/ui/card';
import { Input } from '../components/ui/input';

interface Course { id: number; name: string; semester: string }

interface RosterEntry {
  id: number;
  netid: string;
  full_name: string;
  email: string;
  is_active: boolean;
  imported_at: string;
}

interface UploadResult {
  imported: number;
  updated: number;
  deactivated: number;
  errors: string[];
}

interface MappedRow {
  netid: string;
  full_name: string;
  email: string;
}

interface EditDraft {
  netid: string;
  full_name: string;
  email: string;
  is_active: boolean;
}

// ── Minimal RFC-4180-compliant CSV parser ─────────────────────────────────────
function parseCSV(text: string): string[][] {
  const rows: string[][] = [];
  let row: string[] = [];
  let field = '';
  let inQuotes = false;
  let i = 0;
  while (i < text.length) {
    const ch = text[i];
    if (inQuotes) {
      if (ch === '"') {
        if (text[i + 1] === '"') { field += '"'; i += 2; continue; }
        inQuotes = false;
      } else {
        field += ch;
      }
    } else {
      if (ch === '"') { inQuotes = true; }
      else if (ch === ',') { row.push(field); field = ''; }
      else if (ch === '\n' || ch === '\r') {
        row.push(field); field = '';
        if (row.some((f) => f.trim() !== '')) rows.push(row);
        row = [];
        if (ch === '\r' && text[i + 1] === '\n') i++;
      } else { field += ch; }
    }
    i++;
  }
  if (field !== '' || row.length > 0) {
    row.push(field);
    if (row.some((f) => f.trim() !== '')) rows.push(row);
  }
  return rows;
}

// ── Apply column mapping + transforms to one raw CSV row ─────────────────────
function mapRow(
  rawRow: string[],
  headers: string[],
  netidCol: string,
  nameCol: string,
  emailCol: string,
  skipBlank: boolean,
  stripDomain: boolean,
  reverseName: boolean,
): MappedRow | null {
  const get = (col: string): string => {
    const idx = headers.indexOf(col);
    return idx >= 0 ? (rawRow[idx] ?? '').trim() : '';
  };

  let netid = get(netidCol);
  let full_name = get(nameCol);
  let email = get(emailCol);

  // Strip @domain from netid (Canvas: "NETID@WISC.EDU" → "netid")
  // When netidCol === emailCol, use the full value as email before stripping
  if (stripDomain && netid.includes('@')) {
    if (!email || emailCol === netidCol) email = netid;
    netid = netid.split('@')[0];
  }

  // Extract given name from "Last, First [Middle]" format
  if (reverseName && full_name.includes(',')) {
    full_name = full_name.split(',').slice(1).join(',').trim();
  }

  // Normalize
  netid = netid.toLowerCase();
  email = email.toLowerCase();
  // Title-case each word
  full_name = full_name.replace(/\b\w/g, (c) => c.toUpperCase());

  if (skipBlank && (!netid || !full_name || !email)) return null;
  return { netid, full_name, email };
}

// ── Component ─────────────────────────────────────────────────────────────────
export default function RosterPage() {
  const { courseId } = useParams<{ courseId: string }>();
  const navigate = useNavigate();
  const fileRef = useRef<HTMLInputElement>(null);

  // Heading: "Roster" until the course loads, and if it isn't one of the user's courses (the
  // roster request then fails with its own message).
  const [course, setCourse] = useState<Course | null>(null);

  // Roster list
  const [entries, setEntries] = useState<RosterEntry[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  // Inline edit
  const [editingId, setEditingId] = useState<number | null>(null);
  const [editDraft, setEditDraft] = useState<EditDraft | null>(null);
  const [saving, setSaving] = useState(false);

  // Wizard state
  const [step, setStep] = useState<'idle' | 'mapping' | 'result'>('idle');
  const [fileName, setFileName] = useState('');
  const [headers, setHeaders] = useState<string[]>([]);
  const [rawRows, setRawRows] = useState<string[][]>([]);
  const [netidCol, setNetidCol] = useState('');
  const [nameCol, setNameCol] = useState('');
  const [emailCol, setEmailCol] = useState('');
  const [skipRow2, setSkipRow2] = useState(false);
  const [stripDomain, setStripDomain] = useState(false);
  const [reverseName, setReverseName] = useState(false);
  const [importing, setImporting] = useState(false);
  const [uploadResult, setUploadResult] = useState<UploadResult | null>(null);

  async function load() {
    try {
      const data = await api.get<RosterEntry[]>(`/host/courses/${courseId}/roster`);
      setEntries(data);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load roster');
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => { void load(); }, [courseId]);

  useEffect(() => {
    api.get<Course[]>('/game/my-courses')
      .then((cs) => setCourse(cs.find((c) => String(c.id) === courseId) ?? null))
      .catch(() => {});
  }, [courseId]);

  // ── File selected: parse + auto-detect ───────────────────────────────────
  function handleFileSelect(file: File) {
    setError('');
    const reader = new FileReader();
    reader.onload = (e) => {
      const text = (e.target?.result as string) ?? '';
      const all = parseCSV(text);
      if (all.length === 0) { setError('CSV file appears to be empty.'); return; }

      const hdrs = all[0];
      const data = all.slice(1);
      setFileName(file.name);
      setHeaders(hdrs);
      setRawRows(data);

      // Case-insensitive column finder
      const lower = hdrs.map((h) => h.trim().toLowerCase());
      const find = (...candidates: string[]): string => {
        for (const c of candidates) {
          const idx = lower.indexOf(c);
          if (idx >= 0) return hdrs[idx];
        }
        return '';
      };

      const isCanvas = lower.includes('sis login id');
      const detNetid = find('sis login id', 'netid', 'login id');
      const detName  = find('student', 'full_name', 'full name', 'name');
      const detEmail = find('email');

      setNetidCol(detNetid || hdrs[0] || '');
      setNameCol(detName  || hdrs[1] || '');
      // Canvas: email comes from same column as netid (full "netid@domain" value)
      setEmailCol(detEmail || (isCanvas ? detNetid : hdrs[2]) || '');
      setStripDomain(isCanvas && !!detNetid);
      setReverseName(isCanvas && !!detName);

      // Auto-detect skip row 2: Canvas "Points Possible" metadata row
      const firstDataRow = data[0] ?? [];
      setSkipRow2(firstDataRow.some((cell) =>
        cell.trim().toLowerCase().startsWith('points possible'),
      ));

      setStep('mapping');
    };
    reader.readAsText(file);
  }

  // ── Build mapped rows from current wizard config ──────────────────────────
  function getMappedRows(): MappedRow[] {
    const dataRows = skipRow2 ? rawRows.slice(1) : rawRows;
    const result: MappedRow[] = [];
    for (const row of dataRows) {
      const mapped = mapRow(row, headers, netidCol, nameCol, emailCol, true, stripDomain, reverseName);
      if (mapped) result.push(mapped);
    }
    return result;
  }

  // ── Import ────────────────────────────────────────────────────────────────
  async function handleImport() {
    const rows = getMappedRows();
    if (rows.length === 0) { setError('No valid rows to import with the current mapping.'); return; }
    setImporting(true);
    setError('');
    try {
      const result = await api.post<UploadResult>(
        `/host/courses/${courseId}/roster/import`,
        { rows },
      );
      setUploadResult(result);
      setStep('result');
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Import failed');
    } finally {
      setImporting(false);
    }
  }

  function resetWizard() {
    setStep('idle');
    setFileName('');
    setHeaders([]);
    setRawRows([]);
    setUploadResult(null);
    setError('');
    if (fileRef.current) fileRef.current.value = '';
  }

  // ── Inline edit ───────────────────────────────────────────────────────────
  function startEdit(entry: RosterEntry) {
    setEditingId(entry.id);
    setEditDraft({
      netid: entry.netid,
      full_name: entry.full_name,
      email: entry.email,
      is_active: entry.is_active,
    });
    setError('');
  }

  function cancelEdit() {
    setEditingId(null);
    setEditDraft(null);
  }

  async function saveEdit(entryId: number) {
    if (!editDraft) return;
    setSaving(true);
    setError('');
    try {
      const updated = await api.patch<RosterEntry>(
        `/host/courses/${courseId}/roster/${entryId}`,
        editDraft,
      );
      setEntries((prev) => prev.map((e) => e.id === entryId ? updated : e));
      setEditingId(null);
      setEditDraft(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Update failed');
    } finally {
      setSaving(false);
    }
  }

  // Compute once; only meaningful in mapping step
  const mappedRows = step === 'mapping' ? getMappedRows() : [];
  const previewRows = mappedRows.slice(0, 3);
  const totalRows = mappedRows.length;

  const active = entries.filter((e) => e.is_active);
  const inactive = entries.filter((e) => !e.is_active);

  return (
    <div className="p-8 max-w-5xl">
      <button
        onClick={() => navigate('/rosters')}
        className="flex items-center gap-2 text-slate-400 hover:text-slate-100 text-sm mb-6"
      >
        <ArrowLeft size={14} /> Back to Rosters
      </button>

      <div className="flex items-center justify-between mb-6">
        <h2 className="text-2xl font-bold text-slate-100">
          {course ? `Roster — ${course.name} (${course.semester})` : 'Roster'}
        </h2>
        {step === 'idle' && (
          <>
            <input
              ref={fileRef}
              type="file"
              accept=".csv"
              className="hidden"
              onChange={(e) => { const f = e.target.files?.[0]; if (f) handleFileSelect(f); }}
            />
            <Button size="sm" onClick={() => fileRef.current?.click()}>
              <Upload size={14} className="mr-1" /> Upload CSV
            </Button>
          </>
        )}
      </div>

      {error && <p className="text-red-400 mb-4 text-sm">{error}</p>}

      {/* ── Column-mapping wizard ── */}
      {step === 'mapping' && (
        <Card className="mb-6">
          <CardHeader>
            <div className="flex items-center justify-between">
              <h3 className="text-lg font-semibold text-slate-100">Map Columns — {fileName}</h3>
              <button onClick={resetWizard} className="text-slate-400 hover:text-slate-100"><X size={16} /></button>
            </div>
          </CardHeader>
          <CardContent className="space-y-5">

            {/* Raw CSV preview */}
            <div>
              <p className="text-xs text-slate-400 mb-2">
                Raw preview — {rawRows.length} data row{rawRows.length !== 1 ? 's' : ''} detected:
              </p>
              <div className="overflow-x-auto rounded border border-slate-700">
                <table className="text-xs text-slate-300 w-full">
                  <thead>
                    <tr className="bg-slate-700/50">
                      {headers.map((h, i) => (
                        <th key={i} className="px-3 py-2 text-left font-medium whitespace-nowrap">{h}</th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {rawRows.slice(0, 4).map((row, ri) => (
                      <tr
                        key={ri}
                        className={`border-t border-slate-700/50 ${ri === 0 && skipRow2 ? 'opacity-30 line-through' : ''}`}
                      >
                        {headers.map((_, ci) => (
                          <td key={ci} className="px-3 py-1.5 max-w-[180px] truncate">{row[ci] ?? ''}</td>
                        ))}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>

            {/* Column selectors */}
            <div className="grid grid-cols-3 gap-4">
              {([
                { label: 'NetID column', value: netidCol, set: setNetidCol },
                { label: 'Full name column', value: nameCol, set: setNameCol },
                { label: 'Email column', value: emailCol, set: setEmailCol },
              ] as const).map(({ label, value, set }) => (
                <div key={label}>
                  <label className="text-xs text-slate-400 block mb-1">{label}</label>
                  <select
                    value={value}
                    onChange={(e) => set(e.target.value)}
                    className="w-full bg-slate-800 border border-slate-600 rounded px-2 py-1.5 text-sm text-slate-100 focus:outline-none focus:border-indigo-500"
                  >
                    {headers.map((h) => <option key={h} value={h}>{h}</option>)}
                  </select>
                </div>
              ))}
            </div>

            {/* Options */}
            <div className="space-y-2">
              {([
                {
                  id: 'skip2',
                  checked: skipRow2,
                  set: setSkipRow2,
                  label: 'Skip row 2 — e.g. Canvas "Points Possible" metadata row',
                },
                {
                  id: 'strip',
                  checked: stripDomain,
                  set: setStripDomain,
                  label: 'Strip @domain from NetID column (netid@wisc.edu → netid; full address used as email)',
                },
                {
                  id: 'rev',
                  checked: reverseName,
                  set: setReverseName,
                  label: 'Name is in "Last, First" format — extract given name only',
                },
              ] as const).map(({ id, checked, set, label }) => (
                <label key={id} className="flex items-center gap-2 text-sm text-slate-300 cursor-pointer select-none">
                  <input
                    type="checkbox"
                    checked={checked}
                    onChange={(e) => set(e.target.checked)}
                    className="rounded border-slate-600 bg-slate-800 accent-indigo-500"
                  />
                  {label}
                </label>
              ))}
            </div>
            <p className="text-xs text-slate-500">Rows missing any required field after mapping are automatically skipped.</p>

            {/* Mapped preview */}
            {previewRows.length > 0 && (
              <div>
                <p className="text-xs text-slate-400 mb-2">
                  Mapped preview — {totalRows} row{totalRows !== 1 ? 's' : ''} will be imported:
                </p>
                <div className="rounded border border-slate-700 overflow-hidden">
                  <table className="text-xs text-slate-300 w-full">
                    <thead>
                      <tr className="bg-slate-700/50">
                        <th className="px-3 py-2 text-left">netid</th>
                        <th className="px-3 py-2 text-left">full_name</th>
                        <th className="px-3 py-2 text-left">email</th>
                      </tr>
                    </thead>
                    <tbody>
                      {previewRows.map((r, i) => (
                        <tr key={i} className="border-t border-slate-700/50">
                          <td className="px-3 py-1.5">{r.netid}</td>
                          <td className="px-3 py-1.5">{r.full_name}</td>
                          <td className="px-3 py-1.5">{r.email}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>
            )}

            <div className="flex gap-3">
              <Button onClick={() => void handleImport()} disabled={importing || totalRows === 0}>
                {importing ? 'Importing…' : `Import ${totalRows} row${totalRows !== 1 ? 's' : ''}`}
              </Button>
              <Button variant="ghost" onClick={resetWizard}>Cancel</Button>
            </div>

          </CardContent>
        </Card>
      )}

      {/* ── Result banner ── */}
      {step === 'result' && uploadResult && (
        <Card className="mb-6 p-4 border-green-700 bg-green-900/20">
          <div className="flex items-start justify-between">
            <div>
              <p className="text-green-400 text-sm font-medium">
                Import complete &mdash; {uploadResult.imported} imported, {uploadResult.updated} updated,{' '}
                {uploadResult.deactivated} deactivated
              </p>
              {uploadResult.errors.length > 0 && (
                <ul className="mt-2 text-red-400 text-xs space-y-1">
                  {uploadResult.errors.map((e, i) => <li key={i}>{e}</li>)}
                </ul>
              )}
            </div>
            <button onClick={resetWizard} className="text-slate-400 hover:text-slate-100 ml-4"><X size={16} /></button>
          </div>
        </Card>
      )}

      {/* ── Roster table ── */}
      {loading ? (
        <p className="text-slate-400">Loading…</p>
      ) : entries.length === 0 ? (
        <p className="text-slate-500 text-sm">No roster entries yet. Upload a CSV to get started.</p>
      ) : (
        <>
          <p className="text-slate-400 text-sm mb-4">{active.length} active &middot; {inactive.length} inactive</p>
          <div className="rounded-lg border border-slate-700 overflow-hidden">
            <table className="w-full text-sm">
              <thead>
                <tr className="bg-slate-800/60 text-xs text-slate-400 border-b border-slate-700">
                  <th className="text-left px-4 py-3 font-medium">Full Name</th>
                  <th className="text-left px-4 py-3 font-medium">NetID</th>
                  <th className="text-left px-4 py-3 font-medium">Email</th>
                  <th className="text-left px-4 py-3 font-medium">Status</th>
                  <th className="text-left px-4 py-3 font-medium">Imported</th>
                  <th className="px-4 py-3" />
                </tr>
              </thead>
              <tbody>
                {entries.map((entry) =>
                  editingId === entry.id && editDraft ? (
                    // ── Edit row ──
                    <tr key={entry.id} className="border-t border-slate-700 bg-slate-700/30">
                      <td className="px-3 py-2">
                        <Input
                          value={editDraft.full_name}
                          onChange={(e) => setEditDraft({ ...editDraft, full_name: e.target.value })}
                          className="h-8 text-sm"
                        />
                      </td>
                      <td className="px-3 py-2">
                        <Input
                          value={editDraft.netid}
                          onChange={(e) => setEditDraft({ ...editDraft, netid: e.target.value })}
                          className="h-8 text-sm font-mono"
                        />
                      </td>
                      <td className="px-3 py-2">
                        <Input
                          value={editDraft.email}
                          onChange={(e) => setEditDraft({ ...editDraft, email: e.target.value })}
                          className="h-8 text-sm"
                        />
                      </td>
                      <td className="px-3 py-2">
                        <label className="flex items-center gap-2 cursor-pointer select-none">
                          <input
                            type="checkbox"
                            checked={editDraft.is_active}
                            onChange={(e) => setEditDraft({ ...editDraft, is_active: e.target.checked })}
                            className="rounded border-slate-600 bg-slate-800 accent-indigo-500"
                          />
                          <span className="text-slate-300 text-xs">Active</span>
                        </label>
                      </td>
                      <td className="px-4 py-2 text-slate-500 text-xs whitespace-nowrap">
                        {new Date(entry.imported_at).toLocaleDateString()}
                      </td>
                      <td className="px-3 py-2">
                        <div className="flex gap-2 justify-end">
                          <Button size="sm" onClick={() => void saveEdit(entry.id)} disabled={saving}>
                            {saving ? 'Saving…' : 'Save'}
                          </Button>
                          <Button variant="ghost" size="sm" onClick={cancelEdit} disabled={saving}>
                            Cancel
                          </Button>
                        </div>
                      </td>
                    </tr>
                  ) : (
                    // ── View row ──
                    <tr
                      key={entry.id}
                      className={`border-t border-slate-700/60 hover:bg-slate-800/30 transition-colors ${
                        !entry.is_active ? 'opacity-50' : ''
                      }`}
                    >
                      <td className="px-4 py-3 font-medium text-slate-100">{entry.full_name}</td>
                      <td className="px-4 py-3 font-mono text-xs text-slate-300">{entry.netid}</td>
                      <td className="px-4 py-3 text-slate-400">{entry.email}</td>
                      <td className="px-4 py-3">
                        <span className={`inline-block text-xs font-semibold px-2 py-0.5 rounded-full ${
                          entry.is_active
                            ? 'bg-green-900/50 text-green-400'
                            : 'bg-slate-700 text-slate-400'
                        }`}>
                          {entry.is_active ? 'Active' : 'Inactive'}
                        </span>
                      </td>
                      <td className="px-4 py-3 text-slate-500 text-xs whitespace-nowrap">
                        {new Date(entry.imported_at).toLocaleDateString()}
                      </td>
                      <td className="px-4 py-3 text-right">
                        <Button
                          variant="ghost"
                          size="sm"
                          onClick={() => startEdit(entry)}
                        >
                          <Pencil size={12} className="mr-1" /> Edit
                        </Button>
                      </td>
                    </tr>
                  )
                )}
              </tbody>
            </table>
          </div>
        </>
      )}
    </div>
  );
}
