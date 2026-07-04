import { Endpoint } from "../types.js";
/** Result of listing endpoints, may include a warning for invalid filters */
export interface ListEndpointsResult {
    endpoints: Endpoint[];
    warning?: string;
}
export declare function listEndpoints(category?: string): ListEndpointsResult;
export declare function formatEndpointsTable(result: ListEndpointsResult): string;
