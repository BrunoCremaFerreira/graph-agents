/**
 * The product name the front end says out loud.
 *
 * It lives here, alone and pure, so a rename has exactly one place to happen
 * and a test can reach it: the only other module that spells the name is
 * `renderer.ts`, which needs a GL context and so cannot be unit-tested.
 */

/** The project's name, in the hyphenated form a human reads. */
export const APP_NAME = "rhizome-graph";
