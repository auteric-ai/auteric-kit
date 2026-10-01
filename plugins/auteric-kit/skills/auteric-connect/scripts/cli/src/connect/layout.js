import { existsSync } from 'node:fs';
import { join } from 'node:path';

export const MANAGED_STATE = 'auteric/.state';
export function stateDirectory(root) {
  return existsSync(join(root, MANAGED_STATE, 'connection-status.json')) ||
    existsSync(join(root, MANAGED_STATE, 'config.json')) ? MANAGED_STATE : '.auteric';
}
export function managedPath(root, name) { return join(root, MANAGED_STATE, name); }
