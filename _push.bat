@echo off
chcp 65001 >nul
cd /d "E:\备份管理平台\backup_platform"

echo === fix safe.directory ===
git config --global --add safe.directory "E:/备份管理平台/backup_platform"

echo === create .gitignore ===
echo backups/packages/ > .gitignore
echo *.tar.xz >> .gitignore
echo *.tar.gz >> .gitignore
echo *.gz >> .gitignore
echo logs/ >> .gitignore
echo *.pyc >> .gitignore
echo __pycache__/ >> .gitignore

echo === remove cached large files ===
git rm -r --cached backups/packages/ 2>nul
git rm --cached backups/*/*.gz 2>nul
git rm --cached *.log 2>nul
git rm --cached *.err 2>nul
git rm --cached *.out 2>nul

echo === add all ===
git add -A

echo === amend commit ===
git commit --amend -m "feat: data backup platform - Oracle/MySQL/PG/Kingbase/DM/Redis/MongoDB backup"

echo === push ===
git push -u origin main --force

echo === DONE ===
pause
