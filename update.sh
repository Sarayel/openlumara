#!/bin/bash

# detect Python binary
if command -v python3 >/dev/null 2>&1; then
    PYTHON_BIN="python3"
elif command -v python >/dev/null 2>&1; then
    PYTHON_BIN="python"
else
    echo "error: no python binary found (checked python3 and python)"
    exit 1
fi

echo "checking for updates..."
if git fetch origin 2>/dev/null; then
    if git rev-parse --abbrev-ref @{u} >/dev/null 2>&1; then
        LOCAL=$(git rev-parse HEAD)
        REMOTE=$(git rev-parse @{u})

        if [ "$LOCAL" != "$REMOTE" ]; then
            echo "updates available!"

            # Check if the histories have diverged (force push detected)
            # We check if the remote is NOT an ancestor of local
            if ! git merge-tree $(git merge-base $LOCAL $REMOTE) $LOCAL $REMOTE | grep -q "ancestor"; then
                echo "⚠️ Divergence detected (possible force push). Resetting local branch..."
                git stash
                git reset --hard origin/$(git rev-parse --abbrev-ref @{u})
                git stash pop || echo "note: local changes were preserved in stash but may require manual resolution."
            else
                echo "pulling changes..."
                git stash
                git pull
                git stash pop || echo "note: some local changes could not be automatically reapplied."
            fi
        else
            echo "already up to date."
        fi
    else
        echo "no upstream configured, skipping update check."
    fi
else
    echo "warning: git fetch failed. skipping update check."
fi

if [ ! -d "venv" ]; then
    echo "setting up virtual environment with $PYTHON_BIN..."
    $PYTHON_BIN -m venv venv
fi

echo "ensuring dependencies are up to date..."
source venv/bin/activate
pip install -q --upgrade pip
pip install -r requirements.txt

echo
echo "done!"
