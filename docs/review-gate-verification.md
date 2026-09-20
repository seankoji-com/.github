# Review gate verification

This temporary pull request checks the review gate on a fork contribution.
An approval must apply to the current commit. Dismissing that approval must
return the gate to a waiting state on both the head and test-merge commits.
