import type { Issue } from "../../shared/api/types";

export function issueIsUnread(issue: Issue, acknowledgedRound: number): boolean {
  return !issue.resolved && (!Number.isInteger(issue.round) || issue.round > acknowledgedRound);
}

export function issueMutationsLocked(issues: Issue[], readonly: boolean): boolean {
  // edit-state 的 ack/clear 是整批操作；只要混有 coordinator synthetic evidence 就不能安全修改。
  return readonly || issues.some((issue) => issue.synthetic || issue.read_only);
}
