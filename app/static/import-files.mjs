export const MAX_FILES = 3000;
export const MAX_BYTES = 512 * 1024 * 1024;

export function hiddenPath(path) {
  return path.split(/[\\/]/).some((part) => part.startsWith('.') || part === '__MACOSX');
}

export function classifyFiles(entries) {
  if (!entries.length) throw new Error('No readable files selected. Choose a DICOM folder, ZIP, or NIfTI.');
  if (entries.length > MAX_FILES) throw new Error('Limit this import to 3,000 files.');
  const totalBytes = entries.reduce((total, {file}) => total + file.size, 0);
  if (totalBytes > MAX_BYTES) throw new Error('Limit this import to 512 MB. Split larger batches by series.');
  const nifti = entries.filter(({file}) => /\.nii(\.gz)?$/i.test(file.name));
  if (nifti.length && entries.length !== 1) throw new Error('Import one NIfTI at a time, separately from DICOM files.');
  return {kind: nifti.length ? 'nifti' : 'dicom', totalBytes};
}

export async function collectDirectoryEntries(roots, onProgress = () => {}) {
  const entries = [];
  let bytes = 0, visited = 0;
  async function visit(entry, prefix = '', depth = 0) {
    if (!entry || hiddenPath(entry.name)) return;
    if (++visited > 10000 || depth > 64) throw new Error('Folder is too deeply nested or contains too many entries.');
    const path = prefix + entry.name;
    if (entry.isFile) {
      const file = await new Promise((resolve, reject) => entry.file(resolve, reject));
      entries.push({file, path}); bytes += file.size;
      if (entries.length > MAX_FILES || bytes > MAX_BYTES) throw new Error('Folder exceeds 3,000 files or 512 MB.');
      onProgress(entries.length, bytes);
    } else if (entry.isDirectory) {
      const reader = entry.createReader();
      // Chromium returns directory contents in batches, often only 100 entries at a time.
      while (true) {
        const batch = await new Promise((resolve, reject) => reader.readEntries(resolve, reject));
        if (!batch.length) break;
        for (const child of batch) await visit(child, path + '/', depth + 1);
      }
    }
  }
  for (const root of roots) await visit(root);
  classifyFiles(entries);
  return entries;
}
