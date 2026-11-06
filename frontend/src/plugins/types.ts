import type { ComponentType } from "react";
export interface FrontendPlugin {
  id: string;
  apiVersion: 1;
  pages?: { path: string; title: string; component: ComponentType }[];
  events?: string[];
}
