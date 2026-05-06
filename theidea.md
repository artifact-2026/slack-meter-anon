# Empirically measuring slack resources

The basic idea is that, considering both CPU and I/O as potential bottleneck resources, a system that is bottlenecked on 
one resource should have demonstrable "slack" in the other.  That is, WLOG, if I "saturate" a system by adding mixed load
until such time as offering more load decreases rather than increasing overall throughput, if CPU is indeed the bottleneck resource,
I should be able to demonstrate this by:

 * adding additional CPU-only work, and showing that it decreases the baseline workload's throughput, and
 * measuring HOW MUCH additional I/O work I can add without affecting baseline throughput -- this is the SLACK in that resource.

I should then be able to flip this by making a system I/O-bottlenecked and demonstrating CPU slack.

# Approach

## Containers

We will use Docker containers to create resource-limited environments, both to speed up testing (it takes a long time to saturate my whole laptop)
and obtain ground truth by creating deliberale CPU-bounded containers with plentiful iops (and the reverse).

Because I/O limits do not work on Mac, we will do all of our experiments on EC2, and copy the reports back for analysis.

## Workloads

The core abstraction is the workload, which emulates real applications that stress the resources. A workload can be characterized by a tuple:

(io_mix, intensity)

io_mix is the ratio of I/O to CPU operations, and intensity is a ratio of work operations to total operations.  
When a workload runs, it periodically (eg, every 250ms) chooses a 
new operation in the following way: it picks a random number m 
between 0 and 1; if m > intensity, it chooses a sleep operation 
that yields the CPU.  Otherwise, it chooses a second random 
number n.  If n > io_mix, it chooses an operation that *LOADS 
THE CPU*.  Otherwise, it chooses an operation that *LOADS THE 
I/O SUBSYSTEM*.  It repeats this until it is interrupted.

### Baseline workload

A baseline workload should involve some mix of CPU and I/O operations.  As a default, let's use:

(0.3, 0.75)

This is pretty intense, CPU-heavy workload

### Special workloads

When we measure slack, we will do so by determining how much of one of the following "pure" workloads can be added:


I/O only: (1, ?)
CPU only: (0, ?)

Intensity is left unbound, as this is part of what we need to quantify.

## Generating load

We generate load by placing workload in unix processes (this way they interfere only as the system level).

We can scale load up in two dimensions:

 * by adding more processes running workload
 * by increasing the intensity of workloads in existing threads.

## Measuring throughput

Throughput in our system is just the number of non-sleep operations (so, CPU or I/O) per unit of time.  To measure it, simply record the time a thread starts running
a workload and the time that it is interrupted, as well as the total number of completed non-sleep operations.

# Methodology

Analysis will proceed in the following steps.

## Saturation

Given a baseline workload and a container environment, we offer more of the baseline workload until we can demonstrate that throughput
(successful operations per second) stops increasing and indeed begins to *decrease*.  We can measure this simply as the number
of baseline workload processes required to hit this point of diminishing thoughput.  DO NOT IMPOSE ARTIFICIAL LIMITS.

The final report should include a graph whose x axis is processes and whose y axis is throughput, showing the saturation point and drop-off.

## Slack measurement

If the system is saturated, by assumption, it is bottlenecked on a resource.  Which resource is it?  Show this by systematically attempting to
add first CPU-only work, then I/O-only work.  For each resource R, the methodology is the following:

Add a process running a R-only workload at low intensity (e.g., 0.05).  In an intelligent way (e.g., hybrid binary search), do the following:

Increase the intensity until the baseline throughput at saturation drops.  If you get to and intensity of 1 without a drop, add another process
running the R-only workload at low intensity, and sweep.  The slack measurement will be in the form (THREADS, INTENSITY); E.g,  (3, 0.5) means
throughput dropped only after we added 2 R-only threads at intensity 1, and 1 thread at intensity 0.5.

Report the slack resource and the slack measurement.  Draw reasonable plots.

# Now do it

Once you have the framework built, convince me it works by doing the following:

## CPU-limited container

Set up a severely (fraction of a core) CPU-limited Docker container in ec2.  Run the basic workload and generate a comprehensive report.  Confirm that
it 

1) find a saturation point (in the form of number of baseline processes, and peak throughput)
2) shows that the system is CPU-bottlenecked,
3) measures and report the available I/O slack in the form (PROCESSES, INTENSITY)

## I/O-limited container

Now do the opposite, and confirm the opposite expectation (measurable CPU slack)


## Final comprehensive report.

Assuming all these sanity checks pass, write and open a final HTML report describing what you did and learned.  If the sanity checks do not pass, keep iterating!

# Old crap

If NECESSARY, you may peek in old_shit for evidence of past attempts. But you made TONS of mistakes. remember that this document is gospel.
