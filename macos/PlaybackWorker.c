#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <mach-o/dyld.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

static int redirect_log(const char *state)
{
    int directory = open(state, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
    if (directory < 0)
        return -1;
    struct stat info;
    if (fstat(directory, &info) != 0 || info.st_uid != getuid() || (info.st_mode & 0077)) {
        close(directory);
        return -1;
    }
    int log_file = openat(directory, "session.log", O_WRONLY | O_APPEND | O_NOFOLLOW | O_NONBLOCK | O_CLOEXEC);
    close(directory);
    if (log_file < 0)
        return -1;
    if (log_file <= STDERR_FILENO) {
        int copy = fcntl(log_file, F_DUPFD_CLOEXEC, STDERR_FILENO + 1);
        close(log_file);
        log_file = copy;
    }
    if (log_file < 0)
        return -1;
    int result = fstat(log_file, &info) == 0 && S_ISREG(info.st_mode) && info.st_uid == getuid() &&
        dup2(log_file, STDOUT_FILENO) >= 0 && dup2(log_file, STDERR_FILENO) >= 0;
    close(log_file);
    return result ? 0 : -1;
}

int main(int argc, char **argv)
{
    if (argc != 9 || argv[1][0] != '/' || argv[6][0] != '/' ||
        strcmp(argv[2], "-B") || strcmp(argv[4], "serve") ||
        strcmp(argv[5], "--state-dir") || strcmp(argv[7], "--bundle-root"))
        return 64;
    const char *interpreter = strrchr(argv[1], '/');
    if (interpreter == NULL || strncmp(interpreter + 1, "python", 6))
        return 64;
    if (redirect_log(argv[6]) != 0)
        return 71;

    char executable[PATH_MAX];
    uint32_t size = sizeof(executable);
    if (_NSGetExecutablePath(executable, &size) != 0)
        return 71;
    char *name = strrchr(executable, '/');
    if (name == NULL)
        return 71;
    *name = '\0';

    char relative[PATH_MAX];
    int length = snprintf(relative, sizeof(relative), "%s/../../../../Resources", executable);
    if (length < 0 || (size_t)length >= sizeof(relative))
        return 71;
    char resources[PATH_MAX];
    char requested[PATH_MAX];
    if (realpath(relative, resources) == NULL || realpath(argv[8], requested) == NULL ||
        strcmp(resources, requested))
        return 64;

    char script[PATH_MAX];
    length = snprintf(script, sizeof(script), "%s/player/application.py", resources);
    if (length < 0 || (size_t)length >= sizeof(script))
        return 71;
    if (realpath(argv[3], requested) == NULL || strcmp(script, requested))
        return 64;

    argv[3] = script;
    argv[8] = resources;
    execv(argv[1], argv + 1);
    fprintf(stderr, "Unable to launch the Open SVPflow controller: %s\n", strerror(errno));
    return 71;
}
