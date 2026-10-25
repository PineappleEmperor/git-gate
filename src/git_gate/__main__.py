import sys

if len(sys.argv) == 3 and sys.argv[1] == "call":
    from git_gate.call import main

    main(sys.argv[2])
elif len(sys.argv) >= 2 and sys.argv[1] == "handover":
    from git_gate.handover import main

    sys.exit(main(sys.argv[2:]))
else:
    from git_gate.server import main

    main()
