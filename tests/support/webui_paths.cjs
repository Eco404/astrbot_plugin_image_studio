"use strict";

// Resolve shared paths from this module, independently of a suite's directory.
const path = require("node:path");
const root = path.resolve(__dirname, "../..");
const frontend = path.join(root, "pages", "image-studio");
const fixtures = path.join(root, "tests", "fixtures");
const pagePath = (...parts) => path.join(frontend, ...parts);

module.exports = { root, frontend, fixtures, pagePath };
