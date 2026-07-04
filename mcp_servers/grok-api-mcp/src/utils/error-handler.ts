/**
 * Extract error message from an unknown error value.
 * @param error - The caught error (unknown type)
 * @returns The error message string
 */
export function getErrorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}
