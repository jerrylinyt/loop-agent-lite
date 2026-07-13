/** App-owned mutation lease; the token makes release idempotent across child unmounts. */
export interface OperationToken {
  id: number;
  scope: string;
}

export type BeginOperation = (scope: string) => OperationToken | null;
export type EndOperation = (token: OperationToken) => void;
