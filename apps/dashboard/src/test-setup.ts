import "@testing-library/jest-dom/vitest";
import { afterEach } from "vitest";
import { clearApiCache } from "./api";

// each test mocks its own responses: never reuse another test's
afterEach(() => clearApiCache());
