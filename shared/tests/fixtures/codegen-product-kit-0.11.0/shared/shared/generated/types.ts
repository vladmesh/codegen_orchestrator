// Auto-generated TypeScript types from models.yaml
// DO NOT EDIT MANUALLY

export type UserStatus = "active" | "inactive";

export type UserAccessStatus = "active" | "inactive";

export interface User {
  id: number;
  status?: UserStatus;
  created_at: string;
  updated_at: string;
}

export interface UserChannel {
  id: number;
  user_id: number;
  channel: string;
  external_id: string;
  created_at: string;
  updated_at: string;
}

export interface UserGrant {
  channel: string;
  external_id: string;
}

export interface UserRevoke {
  channel: string;
  external_id: string;
}

export interface UserAccess {
  user_id: number;
  status: UserAccessStatus;
  channel: string;
  external_id: string;
}

export interface CommandReceived {
  command: string;
  args: string[];
  user_id: number;
  timestamp: string;
}

export interface CommandReceivedCreate {
  command: string;
  args: string[];
  user_id: number;
  timestamp: string;
}
