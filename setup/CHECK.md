# Keeping these files true

Go through this list each time a rig update page is published, and whenever something
here stops matching reality.

## At every rig update

1. **The versions block** in REBUILD-THE-RIG.md, part 2: replace it with the new
   `recommended-versions.txt` from the bench, word for word, and update the table under it
   (packages, branches, what isn't on the rig).
2. **The install lines** in REBUILD-THE-RIG.md, part 4: they must name the same
   repositories and branches as the new page's step 4 and step 11b.
3. **Kettles and settings** in REBUILD-THE-RIG.md, part 1: copy any value the rig reported
   changing (its kettle settings files, the Settings it read back).
4. **What's open** in REBUILD-THE-RIG.md (backups, the Pi's model) and AI-SEED.md,
   section 8.
5. **What was built, and why**: add it to AI-SEED.md, section 3.
6. **The release history** in RELEASES.md, part 5: the new rev, its date, what it brought,
   and whether the rig has it.
7. **The repositories** in WORKSTATION.md, part 2: a new plugin, a new rig-followed branch,
   a repository that went private or public.
8. **The coordinator's tools**: in the bench, run
   `& $py coordination\refresh.py` to copy the live tools, briefs, contracts and notes
   into `coordination\`, check `git diff`, and commit.
9. **Run the check**: `test_setup_guide.py` in the bench (through `run_suite.py`, group
   `bench-tools`). It fails while any of 1, 2 or 7 is out of date, or a path these files
   name has gone.
10. **Publish these files.** They are on the core's `master` with everything else. When
    `master` isn't pushed yet (the rig hasn't had the update), push the `setup` folder on
    its own: a branch `setup` made of the pushed `master` plus one commit with this
    folder, pushed to the fork. Once `master` is pushed, `setup` isn't needed.

## What the check looks at

`test_setup_guide.py` in the bench reads these files from
`C:\src\personal\craftbeerpi4\setup` and fails when:

- the versions block in REBUILD-THE-RIG.md differs from the bench's
  `recommended-versions.txt`;
- the repositories and branches in REBUILD-THE-RIG.md's install lines differ from the ones
  in the bench's `rig-update.html`;
- a package the workstation's venv installs from a folder under `C:\src\personal`
  (what `installed_sync.py` checks) has a folder name WORKSTATION.md doesn't mention;
- a path written in backticks under `C:\src\personal\` or `%USERPROFILE%`, or a bench
  path under `coordination\`, doesn't exist, or a link between these files is broken;
- these files hold a personal name, an IP address (other than 127.0.0.1, the PC itself), a
  MAC address or something that looks like a key, or name the GitHub account outside a
  GitHub URL.

A path that is a placeholder (it has `<`, `*` or `…` in it) isn't checked. Write a path
that doesn't exist yet, such as a suggested new folder, without backticks.
