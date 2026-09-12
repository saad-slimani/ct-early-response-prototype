export const STATUS_LABELS = {new: 'New', in_progress: 'In progress', completed: 'Completed', approved: 'Approved'};

export function workflowState({meta, job, jobs, preview, hasBox, modelAvailable, noduleLabel = 1}) {
  const status = meta?.annotation_status;
  const approved = status === 'approved';
  const completed = status === 'completed' || status === 'confirmed';
  const confirmed = completed || approved;
  const current = meta?.history.find((row) => row.id === meta.revision_id);
  const hasMask = (current?.details.volume_ml || 0) > 0;
  const selected = selectedNodule(meta, noduleLabel);
  const selectedHasMask = meta?.nodules ? (selected?.voxel_count || 0) > 0 : hasMask;
  const incomplete = meta?.nodules?.filter((nodule) => !nodule.voxel_count) || [];
  const running = (jobs || [job]).some((item) => ['queued', 'running'].includes(item?.status));
  const freshProposal = proposalIsCurrent(job, meta, noduleLabel);
  let hint = !modelAvailable ? 'Model unavailable. Use Brush to create a manual mask.' :
    !hasBox ? 'Draw a box around one lesion in any plane, then run segmentation.' :
    'Lesion box ready. Run segmentation to generate a proposed mask.';
  if (selectedHasMask) hint = `Inspect ${selected?.id || 'the mask'} in all planes. Edits affect only this nodule.`;
  if (incomplete.length && hasMask) hint += ` Empty: ${incomplete.map((nodule) => nodule.id).join(', ')}. Annotate or delete before completion.`;
  if (meta?.nodules && !selected) hint = 'No nodules remain. Add a nodule to annotate; Undo restores the last deletion.';
  if (freshProposal) hint = 'Model result ready. Inspect the preview, then use it as an editable draft.';
  if (preview) hint = freshProposal ? `Preview for ${selected?.id || 'N1'} only. Other nodules are preserved. Undo restores the previous mask.` :
    'Older model preview. Return to the saved mask; this result cannot replace newer edits.';
  if (running) hint = 'Segmentation is running. You can inspect or edit other nodules without changing this target.';
  if (completed) hint = 'Completed mask locked. Export the case, or review it for approval. Reopen to edit.';
  if (approved) hint = 'Approved revision locked and ready to export. Only a reviewer can reopen or undo approval.';
  return {confirmed, completed, approved, hasMask, selectedHasMask, running, freshProposal, hint,
    step: approved ? 4 : completed ? 3 : (preview || selectedHasMask || freshProposal) && !running ? 2 : 1,
    canConfirm: hasMask && !incomplete.length && !preview && !running && !confirmed && meta?.permissions?.complete !== false,
    canApprove: completed && !!meta?.permissions?.approve,
    canReopen: confirmed && !!meta?.permissions?.reopen,
    canUndoReview: completed || approved && !!meta?.permissions?.undo_approval};
}
import {proposalIsCurrent, selectedNodule} from './nodule-controls.mjs?v=nodules-1';
