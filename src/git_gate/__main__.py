import sys

if len(sys.argv) == 3 and sys.argv[1] == "call":
    from git_gate.call import main

    main(sys.argv[2])
else:
    from git_gate.server import main

    main()
